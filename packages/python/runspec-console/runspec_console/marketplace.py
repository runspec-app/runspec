"""
marketplace.py — the pure core of the Marketplace tab.

The Marketplace lists **runspec runnable packages** an operator can install into
the console's own venvs (its interpreter + every ``[local] venv_globs`` match)
and into the venvs of connected fleet hosts (each host's ``runspec_paths``). The
entries are not curated by hand: they are **generated from package indexes** —
the venv's pip configuration (``pip.ini`` / ``pip.conf`` / ``PIP_*`` env) plus
any ``[[marketplace.repos]]`` declared in ``config.toml`` — by reading each
candidate distribution's core metadata and keeping the ones that declare a
dependency on ``runspec`` (a runnable package always does; a ``*-core`` logic
package or the console itself never counts, see :func:`is_runnable`).

Everything here is stdlib-only and side-effect-free apart from the injected
``fetch`` callable (HTTP) so it unit-tests without a network:

* **pip config discovery** — :func:`pip_config_repos` reads pip's own config
  file chain (global → user → venv, then ``PIP_INDEX_URL`` / ``PIP_EXTRA_INDEX_URL``
  env) and turns ``index-url`` / ``extra-index-url`` into :class:`Repo` entries,
  so a console pointed at a private Nexus / Artifactory / devpi index gets a
  marketplace for that index with **no extra configuration**.
* **index clients** — :class:`IndexClient` speaks the PEP 503 (HTML) and
  PEP 691 (JSON) *simple* API every index implements, plus the pypi.org JSON API
  where available. It lists a repo's project names (streamed name extraction —
  pypi.org's root is ~45 MB / ~900 k names, filtered by the ``runspec-*`` name
  patterns without ever building the full list) and reads a candidate's
  metadata in three tiers: pypi.org JSON API → the PEP 658 ``.metadata``
  sidecar of the newest wheel → the wheel itself (bounded download, METADATA
  read out of the zip, which also proves a bundled ``runspec.toml``).
* **runnable detection** — :func:`is_runnable`: ``Requires-Dist`` names
  ``runspec`` (outside any ``extra ==`` marker) *or* the ``runspec-runnable``
  keyword is set; never a name in the exclude list.
* **install plumbing** — :func:`pip_install_argv` / :func:`pip_uninstall_argv`
  build the ``<venv python> -m pip …`` command a target runs, :func:`pip_env`
  carries the repo as ``PIP_INDEX_URL`` / ``PIP_EXTRA_INDEX_URL`` (with a
  credential's ``user:pass@`` folded into the URL — env, not argv, so a secret
  never shows in ``ps``), and :func:`remote_command` wraps it with ``sudo -n -u``
  / ``su`` for a venv owned by a service account (the same escalation
  :mod:`provision` uses). :data:`INSTALLED_PROBE` is the
  ``python -c`` snippet a target runs to report what it has installed.

Cache: :class:`CatalogCache` persists the last catalogue per repo as JSON with
a TTL so opening the tab is instant and a refresh is explicit.
"""

from __future__ import annotations

import configparser
import fnmatch
import io
import json
import os
import re
import shlex
import sys
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from email.parser import HeaderParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PYPI_SIMPLE_URL = "https://pypi.org/simple/"
"""pip's built-in default index — what an unconfigured venv installs from."""

DEFAULT_PATTERNS: tuple[str, ...] = ("runspec-*",)
"""Name globs a repo scan keeps (matched case-insensitively on the normalised
name). The convention runnable packages follow; a repo can override it."""

DEFAULT_EXCLUDE: tuple[str, ...] = (
    "runspec",
    "runspec-console",
    "runspec-mcp",
    "runspec-room",
    "runspec-registry",
)
"""Distributions that depend on ``runspec`` but are not runnable packages — the
library itself and the apps built on it. Never listed, whatever the metadata."""

RUNNABLE_KEYWORD = "runspec-runnable"
"""A ``Keywords`` entry that marks a package as a runnable even when its
``Requires-Dist`` doesn't say ``runspec`` (a corporate wrapper that pins runspec
through another package, say)."""

DEFAULT_CACHE_TTL_S = 24 * 3600
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_INDEX_BYTES = 128 * 1024 * 1024  # a root index — pypi.org is ~45 MB
DEFAULT_MAX_WHEEL_BYTES = 20 * 1024 * 1024  # tier-3 wheel download cap
DEFAULT_MAX_DESCRIPTION_CHARS = 24_000  # README kept per entry in the cache

PIP_ARGS = ("auto", "none", "index-url", "extra-index-url")
"""How a repo is handed to ``pip install``: ``auto`` derives it from where the
repo came from (pip's ``index-url`` → ``index-url``, an ``extra-index-url`` →
``extra-index-url``, pypi.org → ``none``); ``none`` relies on the target's own
pip config (nothing passed)."""

_USER_AGENT = "runspec-console-marketplace"
_SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
_SIMPLE_ACCEPT = (
    f"{_SIMPLE_JSON}, application/vnd.pypi.simple.v1+html;q=0.2, text/html;q=0.01"
)

FetchResult = tuple[int, str, bytes]
"""``(status, content_type, body)`` — what the injected fetch callable returns."""
Fetch = Callable[..., FetchResult]


class MarketplaceError(Exception):
    """A marketplace operation that could not be completed (network, parse, …)."""


class EmptyProjectError(MarketplaceError):
    """A candidate name resolves to no installable package on the index — a 404
    (not on this index) or a registered-but-empty project (a 200 project page
    with no release files). Common as harmless scan noise: a squatted/typo name
    on public PyPI (e.g. a misspelling of a real ``runspec-*`` package) is
    registered with no uploads, so it appears in the index root listing, matches
    the ``runspec-*`` scan, and reads back empty. :func:`build_catalog` skips such
    a **scanned** candidate silently (nothing to install, nothing the operator
    can fix), but still surfaces it when the operator named it in ``packages``."""


# --------------------------------------------------------------------------- #
# Names + versions (stdlib re-implementation of the bits of `packaging` we need)
# --------------------------------------------------------------------------- #


def normalize_name(name: str) -> str:
    """PEP 503 normalisation: lowercase, runs of ``-_.`` collapsed to ``-``."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


_VERSION_RE = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>\d+)!)?
    (?P<release>\d+(?:\.\d+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>\d*))?
    (?:(?:-(?P<post_n1>\d+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>\d*)))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>\d*))?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)


def version_key(version: str) -> tuple[Any, ...]:
    """A sortable key for a PEP 440 version string.

    Handles epoch, release, pre/post/dev segments (``X.dev < X.a < X.b < X.rc
    < X < X.post``) and ignores local labels. An unparseable string sorts below
    every valid version (so it never wins "latest") but still sorts
    deterministically among its peers.
    """
    m = _VERSION_RE.match(version or "")
    if not m:
        return (0, (-1,), (0, 0), (0, 0), (0, 0), version or "")
    epoch = int(m.group("epoch") or 0)
    release = tuple(int(p) for p in m.group("release").split("."))
    # Trailing zeros don't change a version (1.0 == 1.0.0).
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    pre_l = (m.group("pre_l") or "").lower()
    pre_map = {
        "a": 0,
        "alpha": 0,
        "b": 1,
        "beta": 1,
        "c": 2,
        "rc": 2,
        "pre": 2,
        "preview": 2,
    }
    has_dev = bool(m.group("dev_l"))
    if pre_l:
        pre: tuple[int, int] = (pre_map.get(pre_l, 2), int(m.group("pre_n") or 0))
    elif has_dev and not (m.group("post_l") or m.group("post_n1")):
        pre = (-1, 0)  # a bare dev release sorts below any pre-release
    else:
        pre = (3, 0)  # final
    post_n = m.group("post_n1") or m.group("post_n2")
    post = (
        (1, int(post_n or 0)) if (m.group("post_l") or m.group("post_n1")) else (0, 0)
    )
    dev = (0, int(m.group("dev_n") or 0)) if has_dev else (1, 0)
    return (1, (epoch, *release), pre, post, dev, "")


def is_prerelease(version: str) -> bool:
    m = _VERSION_RE.match(version or "")
    return bool(m and (m.group("pre_l") or m.group("dev_l")))


def newest_version(
    versions: list[str], *, include_prerelease: bool = False
) -> str | None:
    """The highest version in ``versions`` (finals only unless asked, falling
    back to pre-releases when that's all there is)."""
    valid = [v for v in versions if _VERSION_RE.match(v or "")]
    if not valid:
        return None
    finals = [v for v in valid if not is_prerelease(v)]
    pool = valid if include_prerelease or not finals else finals
    return max(pool, key=version_key)


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


_WHEEL_RE = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$"
)
_SDIST_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>\d[^-]*)\.(tar\.gz|zip|tar\.bz2|tar\.xz|tgz)$"
)


def parse_filename(filename: str) -> tuple[str, str, str] | None:
    """``(normalised name, version, kind)`` from a wheel / sdist filename, or
    ``None`` for anything else (an egg, a signature)."""
    m = _WHEEL_RE.match(filename)
    if m:
        return normalize_name(m.group("name")), m.group("version"), "wheel"
    m = _SDIST_RE.match(filename)
    if m:
        return normalize_name(m.group("name")), m.group("version"), "sdist"
    return None


# --------------------------------------------------------------------------- #
# Repos — from config and from pip's own configuration
# --------------------------------------------------------------------------- #


@dataclass
class Repo:
    """One package index the marketplace reads.

    ``source`` is ``"config"`` for a ``[[marketplace.repos]]`` entry and
    ``"pip"`` for one derived from the venv's pip configuration; ``pip_role``
    (``"index"`` / ``"extra"``) records which pip option produced it.
    """

    name: str
    url: str
    enabled: bool = True
    scan: bool = True
    packages: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    credential: str = ""
    pip_arg: str = "auto"
    trusted_host: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "config"
    pip_role: str = ""
    # Resolved at run time by the bridge, never persisted.
    auth: tuple[str, str] | None = None

    @property
    def id(self) -> str:
        return normalize_name(self.name) or normalize_name(self.host)

    @property
    def simple_url(self) -> str:
        return simple_root(self.url)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).hostname or ""

    @property
    def is_pypi(self) -> bool:
        return self.host in ("pypi.org", "www.pypi.org", "pypi.python.org")

    def effective_pip_arg(self) -> str:
        if self.pip_arg and self.pip_arg != "auto":
            return self.pip_arg if self.pip_arg in PIP_ARGS else "none"
        if self.is_pypi:
            return "none"
        return "extra-index-url" if self.pip_role == "extra" else "index-url"

    def effective_patterns(self) -> list[str]:
        return list(self.patterns) if self.patterns else list(DEFAULT_PATTERNS)

    def to_public(self) -> dict[str, Any]:
        """The UI-facing view — the credential *id* only, never a secret."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "scan": self.scan,
            "packages": list(self.packages),
            "patterns": list(self.patterns),
            "credential": self.credential,
            "pip_arg": self.pip_arg,
            "trusted_host": self.trusted_host,
            "tags": list(self.tags),
            "source": self.source,
            "pip_role": self.pip_role,
            "is_pypi": self.is_pypi,
        }


def simple_root(url: str) -> str:
    """The *simple* API root for an index URL — ``.../simple/`` with one
    trailing slash — accepting a bare host, ``/simple`` or ``/simple/``."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    if not u.endswith("/"):
        u += "/"
    parts = urllib.parse.urlsplit(u)
    path = parts.path or "/"
    if not re.search(r"/simple/$", path):
        path = path.rstrip("/") + "/simple/"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def strip_url_auth(url: str) -> tuple[str, tuple[str, str] | None]:
    """Split ``https://user:pass@host/...`` into a clean URL + ``(user, pass)``."""
    parts = urllib.parse.urlsplit(url)
    if not parts.username and not parts.password:
        return url, None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, parts.query, parts.fragment)
    )
    return clean, (
        urllib.parse.unquote(parts.username or ""),
        urllib.parse.unquote(parts.password or ""),
    )


def with_url_auth(url: str, auth: tuple[str, str] | None) -> str:
    if not auth:
        return url
    clean, _ = strip_url_auth(url)
    parts = urllib.parse.urlsplit(clean)
    user, pw = auth
    cred = urllib.parse.quote(user, safe="") + (
        ":" + urllib.parse.quote(pw, safe="") if pw else ""
    )
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            f"{cred}@{parts.netloc}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def repo_display_name(url: str) -> str:
    """A default repo name from its URL — ``pypi.org``, ``nexus.corp:8081/…``."""
    parts = urllib.parse.urlsplit(simple_root(url))
    host = parts.hostname or url
    if host in ("pypi.org", "www.pypi.org"):
        return "PyPI"
    path = re.sub(r"/simple/?$", "", parts.path or "").strip("/")
    return f"{host}/{path}" if path else host


def pip_config_files(
    *,
    platform: str = sys.platform,
    home: str | None = None,
    sys_prefix: str | None = None,
    env: dict[str, str] | None = None,
) -> list[Path]:
    """pip's configuration files in **precedence order** (lowest first):
    global → user → venv (``site``) — then ``PIP_CONFIG_FILE`` which, when set,
    replaces the whole chain (as pip does). Missing files are fine; the caller
    only reads what exists."""
    env_map: dict[str, str] = dict(os.environ) if env is None else env
    home_p = Path(home or os.path.expanduser("~"))
    prefix = Path(sys_prefix or sys.prefix)
    explicit = env_map.get("PIP_CONFIG_FILE")
    if explicit:
        return [] if explicit == os.devnull else [Path(explicit)]
    files: list[Path] = []
    if platform.startswith("win"):
        programdata = env_map.get("PROGRAMDATA") or "C:\\ProgramData"
        appdata = env_map.get("APPDATA") or str(home_p / "AppData" / "Roaming")
        files.append(Path(programdata) / "pip" / "pip.ini")
        files.append(Path(appdata) / "pip" / "pip.ini")
        files.append(home_p / "pip" / "pip.ini")
        files.append(prefix / "pip.ini")
    else:
        files.append(Path("/etc/pip.conf"))
        xdg_dirs = env_map.get("XDG_CONFIG_DIRS") or "/etc/xdg"
        for d in xdg_dirs.split(os.pathsep):
            if d:
                files.append(Path(d) / "pip" / "pip.conf")
        xdg_home = env_map.get("XDG_CONFIG_HOME") or str(home_p / ".config")
        files.append(Path(xdg_home) / "pip" / "pip.conf")
        files.append(home_p / ".pip" / "pip.conf")
        files.append(prefix / "pip.conf")
    return files


def _split_urls(value: str) -> list[str]:
    return [u for u in re.split(r"[\s,]+", value or "") if u.strip()]


def read_pip_config(
    files: list[Path] | None = None,
    *,
    env: dict[str, str] | None = None,
    **kw: Any,
) -> dict[str, Any]:
    """``{"index_url": str|None, "extra_index_urls": [..], "trusted_hosts": [..],
    "cert": str|None, "sources": [paths read]}`` from pip's config chain + env.

    Later files override earlier ones key by key; ``[install]`` overrides
    ``[global]``; ``PIP_INDEX_URL`` / ``PIP_EXTRA_INDEX_URL`` env override all.
    """
    env_map: dict[str, str] = dict(os.environ) if env is None else env
    files = pip_config_files(env=env_map, **kw) if files is None else files
    merged: dict[str, str] = {}
    sources: list[str] = []
    for f in files:
        try:
            if not f.is_file():
                continue
            cp = configparser.RawConfigParser()
            cp.read(f, encoding="utf-8")
        except Exception:
            continue
        sources.append(str(f))
        for section in ("global", "install"):
            if cp.has_section(section):
                for k, v in cp.items(section):
                    merged[k.strip().lower().replace("_", "-")] = v.strip()
    for envk, key in (
        ("PIP_INDEX_URL", "index-url"),
        ("PIP_EXTRA_INDEX_URL", "extra-index-url"),
        ("PIP_TRUSTED_HOST", "trusted-host"),
        ("PIP_CERT", "cert"),
    ):
        if env_map.get(envk):
            merged[key] = env_map[envk]
            sources.append(f"${envk}")
    return {
        "index_url": (merged.get("index-url") or "").strip() or None,
        "extra_index_urls": _split_urls(merged.get("extra-index-url", "")),
        "trusted_hosts": _split_urls(merged.get("trusted-host", "")),
        "cert": (merged.get("cert") or "").strip() or None,
        "sources": sources,
    }


def pip_config_repos(pip_cfg: dict[str, Any] | None = None, **kw: Any) -> list[Repo]:
    """Repos derived from the venv's pip configuration — the primary index
    (pip's default pypi.org when nothing sets one) plus every extra index.
    Embedded ``user:pass@`` is split off into :attr:`Repo.auth` so it is used
    for fetches but never displayed."""
    cfg = read_pip_config(**kw) if pip_cfg is None else pip_cfg
    trusted = {h.lower() for h in cfg.get("trusted_hosts") or []}
    out: list[Repo] = []
    seen: set[str] = set()

    def add(raw: str, role: str) -> None:
        clean, auth = strip_url_auth(raw.strip())
        root = simple_root(clean)
        if not root or root in seen:
            return
        seen.add(root)
        host = urllib.parse.urlsplit(root).hostname or ""
        out.append(
            Repo(
                name=repo_display_name(root),
                url=root,
                enabled=True,
                scan=True,
                source="pip",
                pip_role=role,
                trusted_host=host if host.lower() in trusted else "",
                tags=["pip-config"],
                auth=auth,
            )
        )

    add(cfg.get("index_url") or PYPI_SIMPLE_URL, "index")
    for u in cfg.get("extra_index_urls") or []:
        add(u, "extra")
    return out


def merge_repos(config_repos: list[Repo], pip_repos: list[Repo]) -> list[Repo]:
    """Config entries win over pip-derived ones for the same index root; a
    config entry keeps the pip role (so ``pip_arg = "auto"`` still knows how
    the target's pip would see it)."""
    by_root: dict[str, Repo] = {}
    out: list[Repo] = []
    for r in config_repos:
        by_root[r.simple_url] = r
        out.append(r)
    for p in pip_repos:
        existing = by_root.get(p.simple_url)
        if existing is not None:
            if not existing.pip_role:
                existing.pip_role = p.pip_role
            if existing.auth is None and p.auth is not None:
                existing.auth = p.auth
            if not existing.trusted_host and p.trusted_host:
                existing.trusted_host = p.trusted_host
            continue
        by_root[p.simple_url] = p
        out.append(p)
    return out


def repo_from_dict(raw: dict[str, Any]) -> Repo | None:
    url = str(raw.get("url") or "").strip()
    if not url:
        return None
    name = str(raw.get("name") or "").strip() or repo_display_name(url)
    pip_arg = str(raw.get("pip_arg") or "auto").strip().lower()
    if pip_arg not in PIP_ARGS:
        pip_arg = "auto"
    return Repo(
        name=name,
        url=url,
        enabled=bool(raw.get("enabled", True)),
        scan=bool(raw.get("scan", True)),
        packages=[
            str(p).strip() for p in (raw.get("packages") or []) if str(p).strip()
        ],
        patterns=[
            str(p).strip() for p in (raw.get("patterns") or []) if str(p).strip()
        ],
        credential=str(raw.get("credential") or "").strip(),
        pip_arg=pip_arg,
        trusted_host=str(raw.get("trusted_host") or "").strip(),
        tags=[str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
        source="config",
        pip_role=str(raw.get("pip_role") or "").strip(),
    )


def repo_to_dict(repo: Repo) -> dict[str, Any]:
    """The persisted form of a config repo (no auth, no derived fields)."""
    d: dict[str, Any] = {
        "name": repo.name,
        "url": repo.url,
        "enabled": repo.enabled,
        "scan": repo.scan,
    }
    if repo.packages:
        d["packages"] = list(repo.packages)
    if repo.patterns:
        d["patterns"] = list(repo.patterns)
    if repo.credential:
        d["credential"] = repo.credential
    if repo.pip_arg and repo.pip_arg != "auto":
        d["pip_arg"] = repo.pip_arg
    if repo.trusted_host:
        d["trusted_host"] = repo.trusted_host
    if repo.tags:
        d["tags"] = list(repo.tags)
    return d


def validate_repo(raw: dict[str, Any]) -> str | None:
    """A human error for a malformed repo entry, else ``None``."""
    url = str(raw.get("url") or "").strip()
    if not url:
        return "A repo needs a URL."
    parts = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"Not an http(s) index URL: {url!r}."
    pip_arg = str(raw.get("pip_arg") or "auto").strip().lower()
    if pip_arg not in PIP_ARGS:
        return f"pip_arg must be one of {', '.join(PIP_ARGS)}."
    return None


# --------------------------------------------------------------------------- #
# Core metadata parsing + runnable detection
# --------------------------------------------------------------------------- #

_MULTI_HEADERS = (
    "Requires-Dist",
    "Project-URL",
    "Classifier",
    "Provides-Extra",
    "Requires-External",
)


def parse_core_metadata(text: str) -> dict[str, Any]:
    """Parse a core-metadata (``METADATA`` / PEP 658 sidecar) document into a
    dict of the fields the marketplace uses. Multi-value headers become lists;
    the long description is the body (or the ``Description`` header on old
    metadata versions)."""
    parser = HeaderParser()
    msg = parser.parsestr(text)
    out: dict[str, Any] = {}
    for key in (
        "Name",
        "Version",
        "Summary",
        "Home-page",
        "Author",
        "Author-email",
        "License",
        "License-Expression",
        "Requires-Python",
        "Keywords",
        "Description-Content-Type",
    ):
        v = msg.get(key)
        if v is not None:
            out[key.lower().replace("-", "_")] = str(v).strip()
    for key in _MULTI_HEADERS:
        vals = msg.get_all(key) or []
        out[key.lower().replace("-", "_")] = [
            str(v).strip() for v in vals if str(v).strip()
        ]
    body = msg.get_payload()
    if isinstance(body, str) and body.strip():
        out["description"] = body.strip()
    elif msg.get("Description"):
        out["description"] = str(msg.get("Description")).strip()
    return out


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def requirement_name(requirement: str) -> str:
    """The normalised distribution name at the front of a ``Requires-Dist``."""
    m = _REQ_NAME_RE.match(requirement or "")
    return normalize_name(m.group(1)) if m else ""


def _is_extra_only(requirement: str) -> bool:
    marker = requirement.split(";", 1)[1] if ";" in requirement else ""
    return bool(re.search(r"\bextra\s*==", marker))


def keywords_of(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("keywords")
    if isinstance(raw, list):
        vals = [str(k) for k in raw]
    else:
        vals = re.split(r"[,\s]+", str(raw or ""))
    return [k.strip() for k in vals if k.strip()]


def requires_runspec(meta: dict[str, Any]) -> bool:
    """Does the distribution depend on ``runspec`` outside an extra?"""
    for req in meta.get("requires_dist") or []:
        if requirement_name(req) == "runspec" and not _is_extra_only(req):
            return True
    return False


def is_runnable(
    meta: dict[str, Any], exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE
) -> bool:
    """A runnable package: depends on ``runspec`` (a runnable's ``runspec.toml``
    is parsed by it at run time, so every runnable does) or carries the
    ``runspec-runnable`` keyword — and is not one of the excluded apps/library."""
    name = normalize_name(str(meta.get("name") or ""))
    if not name or name in {normalize_name(e) for e in exclude}:
        return False
    if meta.get("has_runspec_toml"):
        return True
    if RUNNABLE_KEYWORD in {k.lower() for k in keywords_of(meta)}:
        return True
    return requires_runspec(meta)


def is_hidden(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Whether a package ``name`` should be hidden from the catalogue by the
    ``[marketplace] hide`` globs. Unlike :data:`DEFAULT_EXCLUDE` (which marks
    non-runnable distributions), ``hide`` curates *runnable* packages an
    operator/adapter doesn't want offered — e.g. a public package superseded by
    an internal wrapper. Matching is case-insensitive on the PEP 503-normalised
    name, so ``-``/``_`` differences don't matter and a glob works
    (``runspec-jira``, ``runspec-*``)."""
    n = normalize_name(name)
    if not n:
        return False
    return any(fnmatch.fnmatchcase(n, normalize_name(p)) for p in patterns if p)


_URL_LABELS = {
    "documentation": ("documentation", "docs", "doc"),
    "homepage": ("homepage", "home", "home-page", "website", "home page"),
    "source": (
        "source",
        "repository",
        "source code",
        "code",
        "github",
        "gitlab",
        "repo",
    ),
    "changelog": ("changelog", "changes", "release notes", "releases", "history"),
    "issues": ("issues", "bug tracker", "tracker", "bugs", "issue tracker"),
}


def project_links(meta: dict[str, Any]) -> dict[str, str]:
    """``{documentation, homepage, source, changelog, issues}`` from
    ``Project-URL`` entries (``Label, https://…``) + ``Home-page``; keys only
    present when known."""
    links: dict[str, str] = {}
    urls = meta.get("project_urls")
    pairs: list[tuple[str, str]] = []
    if isinstance(urls, dict):
        pairs = [(str(k), str(v)) for k, v in urls.items()]
    else:
        for entry in meta.get("project_url") or []:
            label, _, url = str(entry).partition(",")
            pairs.append((label.strip(), url.strip()))
    for label, url in pairs:
        if not url:
            continue
        low = label.strip().lower()
        for key, aliases in _URL_LABELS.items():
            if low in aliases or any(low.startswith(a) for a in aliases):
                links.setdefault(key, url)
                break
    if meta.get("home_page") and "homepage" not in links:
        links["homepage"] = str(meta["home_page"])
    return links


# --------------------------------------------------------------------------- #
# Simple-index parsing (PEP 503 HTML + PEP 691 JSON)
# --------------------------------------------------------------------------- #


class _AnchorParser(HTMLParser):
    """Collects ``<a href=… data-…>text</a>`` from a simple-index page."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self._cur: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() == "a":
            self._cur = {k.lower(): (v or "") for k, v in attrs}
            self._cur["text"] = ""

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._cur["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._cur is not None:
            self._cur["text"] = self._cur["text"].strip()
            self.anchors.append(self._cur)
            self._cur = None


_JSON_NAME_RE = re.compile(rb'"name"\s*:\s*"([^"\\]{1,200})"')
_HTML_ANCHOR_RE = re.compile(rb"<a\b[^>]*>\s*([^<]{1,200}?)\s*</a>", re.IGNORECASE)


def scan_project_names(
    body: bytes, content_type: str, patterns: list[str]
) -> list[str]:
    """Names in a root simple index that match ``patterns`` (globs on the
    normalised name), extracted by a regex sweep rather than a full JSON/HTML
    parse — pypi.org's root is ~900 k projects and we keep a couple of dozen."""
    pats = [p.lower() for p in patterns if p]
    is_json = "json" in (content_type or "").lower() or body.lstrip()[:1] == b"{"
    rx = _JSON_NAME_RE if is_json else _HTML_ANCHOR_RE
    out: list[str] = []
    seen: set[str] = set()
    for m in rx.finditer(body):
        try:
            raw = m.group(1).decode("utf-8", "replace")
        except Exception:
            continue
        if is_json:
            raw = raw.encode("utf-8").decode("unicode_escape", "ignore")
        name = normalize_name(raw)
        if not name or name in seen:
            continue
        if any(fnmatch.fnmatchcase(name, p) for p in pats):
            seen.add(name)
            out.append(name)
    return out


@dataclass
class IndexFile:
    filename: str
    url: str
    version: str
    kind: str  # wheel | sdist
    yanked: bool = False
    metadata_url: str | None = None
    size: int | None = None
    upload_time: str | None = None
    requires_python: str | None = None


def parse_project_page(body: bytes, content_type: str, page_url: str) -> dict[str, Any]:
    """Files + versions from a per-project simple page (JSON or HTML)."""
    files: list[IndexFile] = []
    versions: list[str] = []
    if "json" in (content_type or "").lower() or body.lstrip()[:1] == b"{":
        data = json.loads(body.decode("utf-8", "replace"))
        versions = [str(v) for v in data.get("versions") or []]
        for f in data.get("files") or []:
            fname = str(f.get("filename") or "")
            parsed = parse_filename(fname)
            if not parsed:
                continue
            _, ver, kind = parsed
            url = urllib.parse.urljoin(page_url, str(f.get("url") or ""))
            meta = f.get("core-metadata", f.get("data-dist-info-metadata"))
            yanked = f.get("yanked")
            files.append(
                IndexFile(
                    filename=fname,
                    url=url,
                    version=ver,
                    kind=kind,
                    yanked=bool(yanked) if not isinstance(yanked, str) else True,
                    metadata_url=(url + ".metadata") if meta else None,
                    size=int(f["size"]) if isinstance(f.get("size"), int) else None,
                    upload_time=str(f.get("upload-time"))
                    if f.get("upload-time")
                    else None,
                    requires_python=str(f.get("requires-python"))
                    if f.get("requires-python")
                    else None,
                )
            )
    else:
        p = _AnchorParser()
        p.feed(body.decode("utf-8", "replace"))
        for a in p.anchors:
            fname = a.get("text") or ""
            parsed = parse_filename(fname)
            if not parsed:
                continue
            _, ver, kind = parsed
            href = a.get("href") or ""
            url = urllib.parse.urljoin(page_url, href.split("#", 1)[0])
            meta = a.get("data-core-metadata", a.get("data-dist-info-metadata"))
            files.append(
                IndexFile(
                    filename=fname,
                    url=url,
                    version=ver,
                    kind=kind,
                    yanked="data-yanked" in a,
                    metadata_url=(url + ".metadata") if meta is not None else None,
                    requires_python=a.get("data-requires-python") or None,
                )
            )
    if not versions:
        seen: list[str] = []
        for f in files:
            if f.version not in seen:
                seen.append(f.version)
        versions = seen
    return {"files": files, "versions": versions}


def pick_latest(
    files: list[IndexFile], versions: list[str], *, include_prerelease: bool = False
) -> tuple[str | None, IndexFile | None]:
    """The newest non-yanked version and its preferred file (wheel over sdist)."""
    live = [f for f in files if not f.yanked]
    candidates = [v for v in versions if any(f.version == v for f in live)] or [
        f.version for f in live
    ]
    latest = newest_version(candidates, include_prerelease=include_prerelease)
    if latest is None:
        return None, None
    of_version = [f for f in live if f.version == latest]
    of_version.sort(
        key=lambda f: (0 if f.kind == "wheel" else 1, 0 if f.metadata_url else 1)
    )
    return latest, (of_version[0] if of_version else None)


def read_wheel_metadata(data: bytes) -> dict[str, Any]:
    """METADATA out of a wheel's bytes, plus ``has_runspec_toml`` — whether the
    wheel ships a ``runspec.toml`` (the definitive runnable marker)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        meta_name = next(
            (
                n
                for n in names
                if n.endswith(".dist-info/METADATA") and n.count("/") == 1
            ),
            None,
        )
        if meta_name is None:
            meta_name = next(
                (n for n in names if n.endswith(".dist-info/METADATA")), None
            )
        if meta_name is None:
            raise MarketplaceError("wheel has no METADATA")
        meta = parse_core_metadata(zf.read(meta_name).decode("utf-8", "replace"))
        meta["has_runspec_toml"] = any(
            n.endswith("/runspec.toml") or n == "runspec.toml" for n in names
        )
        return meta


# --------------------------------------------------------------------------- #
# The index client
# --------------------------------------------------------------------------- #


def default_fetch(
    *,
    ca_bundle: str | None = None,
    tls_verify: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Fetch:
    """A ``fetch(url, headers=…, max_bytes=…, auth=…)`` over urllib honouring
    the machine proxy env and the ``[marketplace]`` CA / TLS knobs."""
    from .webfetch import _build_opener  # shared opener (proxy env + CA bundle)

    import base64
    import urllib.error
    import urllib.request

    opener = _build_opener(ca_bundle, tls_verify)

    def fetch(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int = DEFAULT_MAX_INDEX_BYTES,
        auth: tuple[str, str] | None = None,
    ) -> FetchResult:
        hdrs = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
        hdrs.update(headers or {})
        if auth:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
            hdrs["Authorization"] = f"Basic {token}"
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", None) or resp.getcode() or 0)
                ctype = resp.headers.get("Content-Type", "") or ""
                body = resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise MarketplaceError(
                        f"{url}: response larger than {max_bytes} bytes"
                    )
                return status, ctype, body
        except urllib.error.HTTPError as e:
            return int(e.code), "", b""
        except urllib.error.URLError as e:
            raise MarketplaceError(
                f"Could not reach {url}: {getattr(e, 'reason', e)}"
            ) from e
        except OSError as e:  # includes ssl.SSLError / timeouts
            raise MarketplaceError(f"Could not fetch {url}: {e}") from e

    return fetch


class IndexClient:
    """Reads project names + package metadata from a :class:`Repo`.

    ``fetch`` is injected (see :func:`default_fetch`) so the client is testable
    with canned responses.
    """

    def __init__(
        self,
        fetch: Fetch,
        *,
        max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
        max_wheel_bytes: int = DEFAULT_MAX_WHEEL_BYTES,
        include_prerelease: bool = False,
    ) -> None:
        self._fetch = fetch
        self.max_index_bytes = max_index_bytes
        self.max_wheel_bytes = max_wheel_bytes
        self.include_prerelease = include_prerelease

    # -- names ----------------------------------------------------------------

    def scan_names(self, repo: Repo) -> list[str]:
        """Every project name in the repo's root index matching its patterns."""
        status, ctype, body = self._fetch(
            repo.simple_url,
            headers={"Accept": _SIMPLE_ACCEPT},
            max_bytes=self.max_index_bytes,
            auth=repo.auth,
        )
        if status != 200:
            raise MarketplaceError(f"{repo.name}: root index returned HTTP {status}")
        return scan_project_names(body, ctype, repo.effective_patterns())

    def candidate_names(self, repo: Repo) -> list[str]:
        """Explicit ``packages`` first, then (when ``scan``) the pattern scan."""
        names: list[str] = []
        for p in repo.packages:
            n = normalize_name(p)
            if n and n not in names:
                names.append(n)
        if repo.scan:
            for n in self.scan_names(repo):
                if n not in names:
                    names.append(n)
        return names

    # -- metadata -------------------------------------------------------------

    def project_page(self, repo: Repo, name: str) -> dict[str, Any]:
        url = repo.simple_url + normalize_name(name) + "/"
        status, ctype, body = self._fetch(
            url,
            headers={"Accept": _SIMPLE_ACCEPT},
            max_bytes=self.max_index_bytes,
            auth=repo.auth,
        )
        if status == 404:
            raise EmptyProjectError(f"{name}: not in {repo.name}")
        if status != 200:
            raise MarketplaceError(f"{name}: {repo.name} returned HTTP {status}")
        page = parse_project_page(body, ctype, url)
        page["url"] = url
        return page

    def pypi_json(self, repo: Repo, name: str) -> dict[str, Any] | None:
        """pypi.org's JSON API (``/pypi/{name}/json``) — the richest source
        (README, project URLs, keywords, upload times). ``None`` when the repo
        doesn't serve it."""
        root = re.sub(r"/simple/$", "/", repo.simple_url)
        url = f"{root}pypi/{normalize_name(name)}/json"
        try:
            status, ctype, body = self._fetch(
                url,
                headers={"Accept": "application/json"},
                max_bytes=self.max_index_bytes,
                auth=repo.auth,
            )
        except MarketplaceError:
            return None
        if status != 200 or "json" not in (ctype or "").lower():
            return None
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None
        info = data.get("info") if isinstance(data, dict) else None
        if not isinstance(info, dict) or not info.get("name"):
            return None
        meta: dict[str, Any] = {
            "name": info.get("name"),
            "version": info.get("version"),
            "summary": info.get("summary") or "",
            "description": info.get("description") or "",
            "description_content_type": info.get("description_content_type") or "",
            "keywords": info.get("keywords") or "",
            "requires_dist": list(info.get("requires_dist") or []),
            "requires_python": info.get("requires_python") or "",
            "classifier": list(info.get("classifiers") or []),
            "license": info.get("license") or info.get("license_expression") or "",
            "author": info.get("author") or "",
            "author_email": info.get("author_email") or "",
            "home_page": info.get("home_page") or "",
            "project_urls": dict(info.get("project_urls") or {}),
            "yanked": bool(info.get("yanked")),
        }
        files = data.get("urls") or []
        times = [
            str(f.get("upload_time_iso_8601") or f.get("upload_time") or "")
            for f in files
            if isinstance(f, dict)
        ]
        meta["upload_time"] = max((t for t in times if t), default="")
        rel = data.get("releases")
        if isinstance(rel, dict):
            meta["versions"] = [v for v in rel.keys() if isinstance(v, str)]
        meta["metadata_source"] = "pypi-json"
        return meta

    def fetch_metadata(self, repo: Repo, name: str) -> dict[str, Any]:
        """Core metadata for the newest release of ``name`` in ``repo``.

        Tiers: pypi.org JSON API → PEP 658 ``.metadata`` sidecar of the newest
        wheel → the newest wheel itself (bounded download). Raises
        :class:`MarketplaceError` when none is available.
        """
        meta: dict[str, Any] | None = None
        if repo.is_pypi:
            meta = self.pypi_json(repo, name)
            if (
                meta is not None
                and not self.include_prerelease
                and is_prerelease(str(meta.get("version") or ""))
            ):
                # The JSON API's "latest" may be a pre-release; the simple page
                # below picks the newest final.
                meta = None
        page = self.project_page(repo, name)
        latest, file = pick_latest(
            page["files"], page["versions"], include_prerelease=self.include_prerelease
        )
        if latest is None or file is None:
            raise EmptyProjectError(f"{name}: no releases in {repo.name}")
        if meta is None:
            meta = self._metadata_from_file(repo, name, file)
        meta.setdefault("versions", list(page["versions"]))
        meta["version"] = str(meta.get("version") or latest)
        meta["latest_version"] = latest
        if file.upload_time and not meta.get("upload_time"):
            meta["upload_time"] = file.upload_time
        if file.requires_python and not meta.get("requires_python"):
            meta["requires_python"] = file.requires_python
        meta["wheel"] = file.kind == "wheel"
        return meta

    def _metadata_from_file(
        self, repo: Repo, name: str, file: IndexFile
    ) -> dict[str, Any]:
        if file.metadata_url:
            try:
                status, _ctype, body = self._fetch(
                    file.metadata_url, max_bytes=self.max_index_bytes, auth=repo.auth
                )
            except MarketplaceError:
                status, body = 0, b""
            if status == 200 and body:
                meta = parse_core_metadata(body.decode("utf-8", "replace"))
                meta["metadata_source"] = "pep658"
                return meta
        if file.kind == "wheel" and (
            file.size is None or file.size <= self.max_wheel_bytes
        ):
            status, _ctype, body = self._fetch(
                file.url, max_bytes=self.max_wheel_bytes, auth=repo.auth
            )
            if status != 200:
                raise MarketplaceError(f"{name}: wheel download returned HTTP {status}")
            meta = read_wheel_metadata(body)
            meta["metadata_source"] = "wheel"
            return meta
        raise MarketplaceError(
            f"{name}: {repo.name} publishes no metadata for {file.filename} "
            "(no PEP 658 sidecar and no wheel small enough to inspect)"
        )


# --------------------------------------------------------------------------- #
# Catalogue entries + cache
# --------------------------------------------------------------------------- #


def catalog_entry(
    meta: dict[str, Any],
    repo: Repo,
    *,
    max_description: int = DEFAULT_MAX_DESCRIPTION_CHARS,
) -> dict[str, Any]:
    """The UI-facing entry for one package in one repo."""
    name = normalize_name(str(meta.get("name") or ""))
    desc = str(meta.get("description") or "")
    truncated = len(desc) > max_description
    if truncated:
        desc = desc[:max_description]
    links = project_links(meta)
    versions = [v for v in (meta.get("versions") or []) if isinstance(v, str)]
    return {
        "name": name,
        "display_name": str(meta.get("name") or name),
        "version": str(meta.get("latest_version") or meta.get("version") or ""),
        "versions": sorted(versions, key=version_key, reverse=True)[:20],
        "summary": str(meta.get("summary") or ""),
        "description": desc,
        "description_truncated": truncated,
        "description_content_type": str(meta.get("description_content_type") or ""),
        "keywords": [k for k in keywords_of(meta) if k.lower() != RUNNABLE_KEYWORD],
        "links": links,
        "requires_python": str(meta.get("requires_python") or ""),
        "requires_dist": [
            r for r in (meta.get("requires_dist") or []) if not _is_extra_only(r)
        ],
        "license": str(meta.get("license") or meta.get("license_expression") or ""),
        "author": str(meta.get("author") or meta.get("author_email") or ""),
        "upload_time": str(meta.get("upload_time") or ""),
        "has_runspec_toml": bool(meta.get("has_runspec_toml")),
        "metadata_source": str(meta.get("metadata_source") or ""),
        "repo": repo.id,
        "repo_name": repo.name,
        "repo_url": repo.simple_url,
    }


class CatalogCache:
    """Per-repo catalogue cache: ``{repo_url: {"fetched_at": ts, "entries": [...],
    "errors": [...]}}`` in one JSON file."""

    def __init__(self, path: Path, ttl_s: float = DEFAULT_CACHE_TTL_S) -> None:
        self.path = Path(path)
        self.ttl_s = float(ttl_s)
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = raw if isinstance(raw, dict) else {}
            except Exception:
                self._data = {}
        return self._data

    def get(self, repo_url: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The cached catalogue for a repo if still fresh, else ``None``."""
        entry = self._load().get(simple_root(repo_url))
        if not isinstance(entry, dict):
            return None
        fetched = float(entry.get("fetched_at") or 0)
        if (now if now is not None else time.time()) - fetched > self.ttl_s:
            return None
        return entry

    def get_stale(self, repo_url: str) -> dict[str, Any] | None:
        entry = self._load().get(simple_root(repo_url))
        return entry if isinstance(entry, dict) else None

    def put(
        self,
        repo_url: str,
        entries: list[dict[str, Any]],
        errors: list[str],
        *,
        now: float | None = None,
    ) -> None:
        data = self._load()
        data[simple_root(repo_url)] = {
            "fetched_at": now if now is not None else time.time(),
            "entries": entries,
            "errors": errors,
        }
        self.save()

    def clear(self) -> None:
        self._data = {}
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            self.save()

    def save(self) -> None:
        data = self._load()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def build_catalog(
    client: IndexClient,
    repo: Repo,
    *,
    exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE,
    require_runspec: bool = True,
    fetch_one: Callable[[str], dict[str, Any]] | None = None,
    parallel: Callable[
        [list[str], Callable[[str], dict[str, Any]]],
        list[tuple[str, dict[str, Any] | None, Exception | None]],
    ]
    | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """The catalogue for one repo: ``(entries, errors)``.

    Candidates come from :meth:`IndexClient.candidate_names`; each is read with
    :meth:`IndexClient.fetch_metadata` (``fetch_one`` overrides, ``parallel``
    lets the caller fan the reads out over a thread pool — the default is
    sequential). A candidate is kept when :func:`is_runnable` says so (or
    always, when ``require_runspec`` is False and it was named explicitly).
    """
    names = client.candidate_names(repo)
    explicit = {normalize_name(p) for p in repo.packages}
    read = fetch_one or (lambda n: client.fetch_metadata(repo, n))

    def _run_seq(
        ns: list[str], f: Callable[[str], dict[str, Any]]
    ) -> list[tuple[str, dict[str, Any] | None, Exception | None]]:
        out: list[tuple[str, dict[str, Any] | None, Exception | None]] = []
        for n in ns:
            try:
                out.append((n, f(n), None))
            except Exception as e:  # one bad package never sinks the repo
                out.append((n, None, e))
        return out

    results = (parallel or _run_seq)(names, read)
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, meta, err in results:
        if meta is None:
            # A scanned candidate that resolves to nothing installable — a 404 or
            # a registered-but-empty project (a squatted/typo name on public PyPI
            # matching the runspec-* scan reads back empty) — is discovery noise,
            # not something the operator can act on, so skip it silently. Keep the
            # warning when the operator named it in `packages` (a typo/empty is
            # worth flagging there), and for every other read failure (a real
            # network/parse problem). A str err (a `parallel` on the old contract)
            # never matches, so it degrades to the warning.
            if isinstance(err, EmptyProjectError) and name not in explicit:
                continue
            msg = str(err) if err is not None else "could not be read"
            errors.append(msg if msg.startswith(f"{name}:") else f"{name}: {msg}")
            continue
        meta.setdefault("name", name)
        if is_runnable(meta, exclude) or (not require_runspec and name in explicit):
            entries.append(catalog_entry(meta, repo))
        elif name in explicit and normalize_name(str(meta.get("name") or name)) not in {
            normalize_name(e) for e in exclude
        }:
            errors.append(
                f"{name}: listed in the repo's packages but does not depend on runspec (add the '{RUNNABLE_KEYWORD}' keyword or set require_runspec = false)"
            )
    entries.sort(key=lambda e: e["name"])
    return entries, errors


# --------------------------------------------------------------------------- #
# Targets (venvs) + installed-state probe
# --------------------------------------------------------------------------- #


def venv_python(runspec_path: str) -> str:
    """The interpreter of the venv a ``runspec`` binary lives in.

    Accepts the binary (``…/bin/runspec`` / ``…\\Scripts\\runspec.exe``) or the
    venv root; returns ``…/bin/python`` / ``…\\Scripts\\python.exe``.
    """
    p = (runspec_path or "").strip()
    if not p:
        return ""
    win = "\\" in p or p.lower().endswith(".exe")
    sep = "\\" if win else "/"
    parts = (
        p.rstrip("/\\").split(sep) if not win else re.split(r"[\\/]", p.rstrip("/\\"))
    )
    last = parts[-1].lower() if parts else ""
    if last in ("runspec", "runspec.exe"):
        parts = parts[:-1]
        last = parts[-1].lower() if parts else ""
    if last not in ("bin", "scripts"):
        parts = [*parts, "Scripts" if win else "bin"]
    return sep.join([*parts, "python.exe" if win else "python"])


def venv_root(runspec_path: str) -> str:
    py = venv_python(runspec_path)
    sep = "\\" if "\\" in py else "/"
    parts = py.split(sep)
    return sep.join(parts[:-2]) if len(parts) > 2 else py


INSTALLED_PROBE = (
    "import json,sys,os,importlib.metadata as m\n"
    "d={}\n"
    "for x in m.distributions():\n"
    "    try:\n"
    "        n=x.metadata['Name']\n"
    "    except Exception:\n"
    "        continue\n"
    "    if n: d[n.lower().replace('_','-').replace('.','-')]=x.version\n"
    "o=''\n"
    "try:\n"
    "    import pwd;o=pwd.getpwuid(os.stat(sys.prefix).st_uid).pw_name\n"
    "except Exception:\n"
    "    pass\n"
    "u=''\n"
    "try:\n"
    "    u=os.getlogin()\n"
    "except Exception:\n"
    "    u=os.environ.get('USER') or os.environ.get('USERNAME') or ''\n"
    "print('RSMP'+json.dumps({'prefix':sys.prefix,'python':sys.version.split()[0],'owner':o,'user':u,'dists':d}))\n"
)
"""A ``python -c`` program run **with the target venv's interpreter**: prints
one ``RSMP{…}`` line with every installed distribution (normalised name →
version), the venv owner (POSIX) and the invoking user."""


def probe_argv(python: str) -> list[str]:
    return [python, "-c", INSTALLED_PROBE]


def parse_probe_output(text: str) -> dict[str, Any] | None:
    """The ``RSMP{…}`` record out of a probe's stdout (tolerates noise)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("RSMP{"):
            try:
                data = json.loads(line[4:])
            except ValueError:
                return None
            if isinstance(data, dict) and isinstance(data.get("dists"), dict):
                return data
    return None


# --------------------------------------------------------------------------- #
# pip command building
# --------------------------------------------------------------------------- #

_PKG_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?([<>=!~]=?[A-Za-z0-9.*+!-]+(,[<>=!~]=?[A-Za-z0-9.*+!-]+)*)?$"
)


def validate_spec(spec: str) -> str:
    """A pip requirement token (``name``, ``name[extra]``, ``name==1.2``) — the
    shape a plain ``pip install`` takes; anything else (a URL, a path, an
    option) is refused so the marketplace can only install
    named packages from the configured index."""
    s = (spec or "").strip()
    if not s or s.startswith("-") or not _PKG_TOKEN_RE.match(s):
        raise MarketplaceError(f"not a package spec: {spec!r}")
    return s


def pip_install_argv(
    python: str,
    specs: list[str],
    *,
    upgrade: bool = False,
    reinstall: bool = False,
    pre: bool = False,
    no_cache: bool = False,
) -> list[str]:
    """``<python> -m pip install [-U] [--force-reinstall] [--pre] spec…``."""
    cleaned = [validate_spec(s) for s in specs]
    if not cleaned:
        raise MarketplaceError("nothing to install")
    argv = [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if upgrade:
        argv.append("-U")
    if reinstall:
        argv.append("--force-reinstall")
    if pre:
        argv.append("--pre")
    if no_cache:
        argv.append("--no-cache-dir")
    return [*argv, *cleaned]


def pip_uninstall_argv(python: str, names: list[str]) -> list[str]:
    cleaned = [validate_spec(n) for n in names]
    if not cleaned:
        raise MarketplaceError("nothing to uninstall")
    return [
        python,
        "-m",
        "pip",
        "uninstall",
        "-y",
        "--disable-pip-version-check",
        *cleaned,
    ]


def pip_env(repo: Repo | None, auth: tuple[str, str] | None = None) -> dict[str, str]:
    """Env vars that point ``pip install`` at the repo — ``PIP_INDEX_URL`` /
    ``PIP_EXTRA_INDEX_URL`` per :meth:`Repo.effective_pip_arg` (nothing for
    ``none``, i.e. pypi.org or "use the target's own pip config"), plus
    ``PIP_TRUSTED_HOST`` when the repo is a trusted host. A credential rides
    inside the URL (env, not argv — it never shows in ``ps``)."""
    if repo is None:
        return {}
    arg = repo.effective_pip_arg()
    env: dict[str, str] = {}
    if arg == "none":
        return env
    url = with_url_auth(repo.simple_url, auth or repo.auth)
    env["PIP_INDEX_URL" if arg == "index-url" else "PIP_EXTRA_INDEX_URL"] = url
    if repo.trusted_host:
        env["PIP_TRUSTED_HOST"] = repo.trusted_host
    return env


def exec_argv(
    argv: list[str],
    *,
    run_as: str = "",
    become_method: str = "sudo",
    env: dict[str, str] | None = None,
) -> tuple[list[str], bytes | None]:
    """``argv`` escalated for a service-account venv (``sudo -n -u user`` / ``su``,
    the same shape :mod:`provision` builds) with ``env`` carried
    as the SPEC env frame: returns ``(argv, stdin)``. The env holds the index URL,
    which can embed a credential, so it never goes on the command line (every user
    can read that in ``ps``, and ``sudo`` logs it) — the caller writes ``stdin``
    to the process first."""
    from runspec.become import build_exec_argv  # core, stdlib

    method = (become_method or "sudo").strip().lower()
    flags = "-n" if run_as and method == "sudo" else None
    return build_exec_argv(
        list(argv), run_as, method, become_flags=flags, env=env or None
    )


def remote_command(
    argv: list[str],
    *,
    run_as: str = "",
    become_method: str = "sudo",
    env: dict[str, str] | None = None,
) -> tuple[str, bytes | None]:
    """One quoted shell line for an SSH exec channel plus the stdin to feed it —
    :func:`exec_argv`, joined. Every token is quoted, so a URL with a credential
    in it could never be split by the shell (and it isn't in the line anyway)."""
    parts, frame = exec_argv(argv, run_as=run_as, become_method=become_method, env=env)
    return " ".join(shlex.quote(p) for p in parts), frame


def summarize_pip_output(output: str, *, limit: int = 12) -> str:
    """The last few meaningful lines of a pip run for a result card."""
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    keep = [
        ln
        for ln in lines
        if not ln.lstrip().startswith(
            (
                "Requirement already satisfied",
                "Collecting",
                "Downloading",
                "Using cached",
                "  Preparing",
                "  Attempting",
            )
        )
    ]
    tail = (keep or lines)[-limit:]
    return "\n".join(tail)


def install_state(installed: str | None, available: str | None) -> str:
    """``absent`` / ``current`` / ``outdated`` / ``newer`` / ``unknown``."""
    if not installed:
        return "absent"
    if not available:
        return "unknown"
    c = compare_versions(installed, available)
    return "current" if c == 0 else ("outdated" if c < 0 else "newer")
