"""Package repositories: list, add (with a GPG key) and remove.

Covers apt (Debian/Ubuntu), dnf/yum (RHEL/Fedora/CentOS) and zypper (SUSE);
pacman hosts are refused. A repo is added the way vendor install guides do it
(Grafana's Alloy guide is the model), without a shell:

- **apt** — the key goes to ``/etc/apt/keyrings/<name>.asc`` and a one-line
  ``/etc/apt/sources.list.d/<name>.list`` names it with ``signed-by=``, so the
  key is trusted for that repo only.
- **dnf / yum** — the key goes to ``/etc/pki/rpm-gpg/RPM-GPG-KEY-<name>``, is
  imported with ``rpm --import``, and ``/etc/yum.repos.d/<name>.repo`` points
  ``gpgkey=`` at the local file with ``gpgcheck=1``.
- **zypper** — the same key file and import; the repo file is
  ``/etc/zypp/repos.d/<name>.repo``.

One call carries the apt *and* the rpm details, and each host uses the pair for
its own package manager, so one call works across a mixed group of hosts.

``add`` downloads the key itself (``https`` only; ``http`` only with a pinned
fingerprint; or a file already on the host), reports every key's fingerprint,
and with ``key_fingerprints`` refuses a key file holding any key not on that
list. It then refreshes just that repo and puts everything back if the refresh
fails. Files it writes start with a marker line; a same-named file without the
marker (a repo someone added by hand) is only overwritten with ``replace``.
Everything here needs root except ``list_repos``.
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from runspec_linux_core import pgpkey
from runspec_linux_core.errors import CommandError, LinuxCoreError
from runspec_linux_core.packages import _run, _tail, detect_manager

# Tests point this at a temporary directory; every path below is joined to it.
_ROOT = "/"

MARKER = "# Managed by runspec package-repo. 'package-repo add' rewrites this file; 'package-repo remove' deletes it."

_APT_SOURCES_DIR = "etc/apt/sources.list.d"
_APT_SOURCES_LIST = "etc/apt/sources.list"
_APT_KEYRINGS = "etc/apt/keyrings"
_YUM_REPOS = "etc/yum.repos.d"
_ZYPP_REPOS = "etc/zypp/repos.d"
_RPM_KEYS = "etc/pki/rpm-gpg"

_MAX_KEY_BYTES = 1024 * 1024

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUITE = re.compile(r"^[A-Za-z0-9./][A-Za-z0-9._/-]*$")
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_UNSAFE_URL_CHARS = re.compile(r"[\s\"'\[\]<>\\`]")

_APT_FAILURE = ("NO_PUBKEY", "EXPKEYSIG", "is not signed", "GPG error", "Failed to fetch", "The repository", "Conflicting values")


def _path(rel: str) -> Path:
    return Path(_ROOT) / rel


def _family(mgr: str) -> str:
    if mgr == "apt-get":
        return "apt"
    if mgr in ("dnf", "yum", "zypper"):
        return "rpm"
    raise LinuxCoreError(f"adding package repositories isn't supported with {mgr}")


def validate_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"invalid repo name {name!r}: letters, digits, '.', '_' and '-' only, starting with a letter or digit")
    return name


def _validate_repo_url(url: str, what: str) -> str:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError(f"{what} must be an https:// (or http://) URL, got {url!r}")
    if _UNSAFE_URL_CHARS.search(url):
        raise ValueError(f"{what} contains a space, quote or bracket: {url!r}")
    return url


def _validate_key_source(src: str, pinned: bool) -> str:
    if src.startswith("/"):
        return src
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme == "https" and parsed.netloc:
        return src
    if parsed.scheme == "http" and parsed.netloc:
        if not pinned:
            raise ValueError("a key from an http:// URL can be swapped in transit — use https://, or pin it with --key-fingerprint")
        return src
    raise ValueError(f"key must be an https:// URL or an absolute file path on the host, got {src!r}")


# ── reading what's configured ─────────────────────────────────────────────────


def _parse_apt_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = re.match(r"^(deb|deb-src)\s+(?:\[([^\]]*)\]\s+)?(\S+)\s+(\S+)\s*(.*)$", line)
    if not m:
        return None
    options = dict(opt.split("=", 1) for opt in (m.group(2) or "").split() if "=" in opt)
    return {
        "type": m.group(1),
        "urls": [m.group(3)],
        "suites": [m.group(4)],
        "components": m.group(5).split(),
        "signed_by": options.get("signed-by", ""),
        "enabled": True,
    }


def _parse_deb822(text: str) -> list[dict]:
    entries = []
    for para in re.split(r"\n\s*\n", text):
        fields: dict[str, str] = {}
        last = None
        for raw in para.splitlines():
            if raw.startswith("#"):
                continue
            if raw[:1] in (" ", "\t") and last:
                fields[last] += "\n" + raw.strip()
                continue
            if ":" in raw:
                key, value = raw.split(":", 1)
                last = key.strip().lower()
                fields[last] = value.strip()
        if "uris" not in fields:
            continue
        signed_by = fields.get("signed-by", "")
        entries.append(
            {
                "type": fields.get("types", "deb"),
                "urls": fields["uris"].split(),
                "suites": fields.get("suites", "").split(),
                "components": fields.get("components", "").split(),
                "signed_by": "(key inside the file)" if "BEGIN PGP" in signed_by else signed_by,
                "enabled": fields.get("enabled", "yes").lower() not in ("no", "false", "0"),
            }
        )
    return entries


def _apt_entries() -> list[dict]:
    files: list[Path] = []
    main = _path(_APT_SOURCES_LIST)
    if main.is_file():
        files.append(main)
    src_dir = _path(_APT_SOURCES_DIR)
    if src_dir.is_dir():
        files += sorted(p for p in src_dir.iterdir() if p.suffix in (".list", ".sources") and p.is_file())
    out: list[dict] = []
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        managed = text.startswith(MARKER)
        found = _parse_deb822(text) if f.suffix == ".sources" else [e for e in map(_parse_apt_line, text.splitlines()) if e]
        for entry in found:
            out.append({"name": f.stem, "file": str(f), "managed": managed, **entry})
    return out


def _ini_entries(rel_dir: str) -> list[dict]:
    repo_dir = _path(rel_dir)
    if not repo_dir.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(p for p in repo_dir.iterdir() if p.suffix == ".repo" and p.is_file()):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text)
        except configparser.Error:
            out.append({"name": f.stem, "file": str(f), "error": "could not parse"})
            continue
        for section in parser.sections():
            s = parser[section]
            urls = (s.get("baseurl") or "").split()
            out.append(
                {
                    "name": f.stem,
                    "id": section,
                    "file": str(f),
                    "managed": text.startswith(MARKER),
                    "urls": urls,
                    "mirrorlist": s.get("mirrorlist") or s.get("metalink") or "",
                    "enabled": (s.get("enabled") or "1").strip() not in ("0", "no", "false"),
                    "gpgcheck": (s.get("gpgcheck") or "").strip() in ("1", "yes", "true"),
                    "gpgkey": (s.get("gpgkey") or "").split(),
                }
            )
    return out


def list_repos(name_filter: str | None = None) -> dict:
    """Return the package repositories configured on this host.

    Reads the repo files only (no root, no network). ``name_filter`` keeps the
    entries whose name, id or URL contains that text. Returns
    ``{manager, count, repos}``; each repo has ``name`` (the file name without
    its extension, which is what ``remove_repo`` takes), ``file``, ``urls``,
    ``enabled``, ``managed`` (added by ``add_repo``) and the manager's own
    details (apt: suites/components/signed_by; rpm: id/gpgcheck/gpgkey).
    """
    mgr = detect_manager()
    family = _family(mgr)
    repos = _apt_entries() if family == "apt" else _ini_entries(_ZYPP_REPOS if mgr == "zypper" else _YUM_REPOS)
    if name_filter:
        needle = name_filter.lower()
        repos = [r for r in repos if needle in r["name"].lower() or needle in str(r.get("id", "")).lower() or any(needle in u.lower() for u in r.get("urls", []))]
    return {"manager": mgr, "count": len(repos), "repos": repos}


# ── adding ────────────────────────────────────────────────────────────────────


def _fetch_key(src: str, *, proxy: str | None, ca_bundle: str | None, timeout: float) -> bytes:
    if src.startswith("/"):
        try:
            with open(src, "rb") as fh:
                data = fh.read(_MAX_KEY_BYTES + 1)
        except OSError as exc:
            raise LinuxCoreError(f"can't read key file {src}: {exc.strerror or exc}") from exc
    else:
        handlers: list[urllib.request.BaseHandler] = []
        if ca_bundle:
            try:
                ctx = ssl.create_default_context(cafile=ca_bundle)
            except (OSError, ssl.SSLError) as exc:
                raise LinuxCoreError(f"can't load CA bundle {ca_bundle}: {exc}") from exc
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if proxy == "direct":
            handlers.append(urllib.request.ProxyHandler({}))
        elif proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(src, headers={"User-Agent": "runspec-linux-core package-repo"})
        try:
            with opener.open(request, timeout=timeout) as resp:
                data = resp.read(_MAX_KEY_BYTES + 1)
        except OSError as exc:  # URLError, HTTPError, timeouts, TLS failures
            reason = getattr(exc, "reason", None) or exc
            raise LinuxCoreError(f"couldn't download the key from {src}: {reason}") from exc
    if len(data) > _MAX_KEY_BYTES:
        raise LinuxCoreError(f"key from {src} is larger than 1 MB — that isn't a key file")
    return data


def _check_pins(keys: list[pgpkey.PublicKey], pins: list[str]) -> None:
    if not pins:
        return
    wanted = {pgpkey.normalize_fingerprint(p) for p in pins}
    unpinned = [k.fingerprint for k in keys if k.fingerprint not in wanted]
    if unpinned:
        raise LinuxCoreError(
            "the key file holds key(s) you didn't pin: " + ", ".join(unpinned) + ". Nothing was changed. Check the fingerprints with the vendor before adding them to --key-fingerprint."
        )


def _armored(keys: list[pgpkey.PublicKey]) -> bytes:
    # One armor block per key: older rpm imports only the first key of a block.
    return b"".join(pgpkey.armor(k.packets) for k in keys)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.runspec-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _restore(saved: dict[Path, bytes | None]) -> None:
    for path, old in saved.items():
        try:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _write(path, old)
        except OSError:
            pass


def _norm_url(url: str) -> str:
    return url.rstrip("/").lower()


def _apt_files(name: str, url: str, suite: str, components: list[str], armored: bytes) -> dict[Path, bytes]:
    key_path = _path(_APT_KEYRINGS) / f"{name}.asc"
    signed_by = f"/{_APT_KEYRINGS}/{name}.asc"  # the path on the host, even when _ROOT is a test dir
    line = " ".join(["deb", f"[signed-by={signed_by}]", url, suite, *components])
    source = f"{MARKER}\n{line}\n".encode()
    return {key_path: armored, _path(_APT_SOURCES_DIR) / f"{name}.list": source}


def _rpm_files(mgr: str, name: str, url: str, armored: bytes, signed_metadata: bool) -> dict[Path, bytes]:
    key_path = _path(_RPM_KEYS) / f"RPM-GPG-KEY-{name}"
    key_ref = f"/{_RPM_KEYS}/RPM-GPG-KEY-{name}"
    lines = [
        MARKER,
        f"[{name}]",
        f"name={name}",
        f"baseurl={url}",
        "enabled=1",
        "gpgcheck=1",
        f"repo_gpgcheck={1 if signed_metadata else 0}",
        f"gpgkey=file://{key_ref}",
    ]
    if mgr == "zypper":
        lines += ["autorefresh=1", "type=rpm-md"]
        repo_path = _path(_ZYPP_REPOS) / f"{name}.repo"
    else:
        lines += ["sslverify=1"]
        repo_path = _path(_YUM_REPOS) / f"{name}.repo"
    return {key_path: armored, repo_path: ("\n".join(lines) + "\n").encode()}


def _rpm_import(armored_key: bytes) -> subprocess.CompletedProcess[str]:
    fd, tmp = tempfile.mkstemp(prefix="runspec-key-", suffix=".asc")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(armored_key)
        return _run(["rpm", "--import", tmp])
    finally:
        os.unlink(tmp)


_DNF_CACHE_DIR = re.compile(r"^(?P<name>.+)-[0-9a-f]{16}$")


def _forget_repo_keyring(mgr: str, name: str) -> None:
    """Delete dnf's / yum's cached per-repo keyring for ``name`` (rebuilt from gpgkey= on the next refresh)."""
    dirs: list[Path] = []
    if mgr == "dnf":
        cache = _path("var/cache/dnf")
        if cache.is_dir():
            for d in cache.iterdir():
                m = _DNF_CACHE_DIR.match(d.name)
                if m and m.group("name") == name:
                    dirs.append(d / "pubring")
    elif mgr == "yum":
        dirs += [p for p in _path("var/lib/yum/repos").glob(f"*/*/{name}/gpgdir*")]
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def _rpm_has_key(rpm_name: str) -> bool:
    return subprocess.run(["rpm", "-q", rpm_name], capture_output=True, text=True).returncode == 0


def _refresh(mgr: str, name: str) -> subprocess.CompletedProcess[str]:
    if mgr == "apt-get":
        cmd = [
            "apt-get",
            "update",
            "-q",
            "-o",
            f"Dir::Etc::sourcelist=sources.list.d/{name}.list",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "APT::Get::List-Cleanup=0",
        ]
    elif mgr == "zypper":
        cmd = ["zypper", "--non-interactive", "refresh", name]
    elif mgr == "dnf":
        # --refresh: a cache from a moment ago would otherwise skip the download (and the check).
        cmd = ["dnf", "-y", f"--repo={name}", "makecache", "--refresh"]
    else:  # yum (CentOS/RHEL 7) has no --refresh: expire the repo's cache first.
        only = ["--disablerepo=*", f"--enablerepo={name}"]
        expired = _run(["yum", "-y", *only, "clean", "expire-cache"])
        if expired.returncode != 0:
            return expired
        cmd = ["yum", "-y", *only, "makecache"]
    return _run(cmd)


def _refresh_failed(mgr: str, result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0:
        return True
    if mgr == "apt-get":
        # Older apt exits 0 when a repo's signature or download fails.
        text = result.stdout + result.stderr
        return any(marker in text for marker in _APT_FAILURE)
    return False


def add_repo(
    name: str,
    *,
    apt_url: str | None = None,
    apt_key: str | None = None,
    apt_suite: str = "stable",
    apt_components: str = "main",
    rpm_url: str | None = None,
    rpm_key: str | None = None,
    key_fingerprints: list[str] | None = None,
    signed_metadata: bool = True,
    replace: bool = False,
    refresh: bool = True,
    proxy: str | None = None,
    ca_bundle: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Add (or update) a package repository and its signing key. Needs root.

    Give the apt pair (``apt_url`` + ``apt_key``), the rpm pair (``rpm_url`` +
    ``rpm_key``, used by dnf, yum and zypper), or both; the host uses the pair
    for its own package manager and a missing pair is a ``ValueError`` naming
    it. A key is an ``https://`` URL or an absolute path on the host.

    Returns ``{manager, name, changed, refreshed, keys, files, output}``.
    ``changed`` is false when every file already had this content (nothing is
    written or refreshed). ``keys`` lists each primary key's fingerprint and
    creation date. Raises ``ValueError`` on bad input, ``LinuxCoreError`` when
    the key can't be fetched, isn't a key, isn't pinned, or a same-named repo
    exists that this didn't add (unless ``replace``), and ``CommandError`` when
    the key import or the refresh fails (after putting the old files back).
    """
    validate_name(name)
    mgr = detect_manager()
    family = _family(mgr)
    pins = [p for p in (key_fingerprints or []) if p.strip()]
    for pin in pins:
        pgpkey.normalize_fingerprint(pin)

    if family == "apt":
        if not (apt_url and apt_key):
            raise ValueError(f"this host uses apt: give --apt-url and --apt-key for the '{name}' repo")
        url, key_src = _validate_repo_url(apt_url, "--apt-url"), apt_key
        if not _SUITE.match(apt_suite or ""):
            raise ValueError(f"invalid --apt-suite {apt_suite!r}")
        components = (apt_components or "").split()
        if apt_suite.endswith("/"):
            if components:
                raise ValueError("a flat repo (--apt-suite ending in '/') takes no --apt-components")
        elif not components:
            raise ValueError("--apt-components can't be empty (e.g. 'main')")
        for comp in components:
            if not _COMPONENT.match(comp):
                raise ValueError(f"invalid apt component {comp!r}")
    else:
        if not (rpm_url and rpm_key):
            raise ValueError(f"this host uses {mgr}: give --rpm-url and --rpm-key for the '{name}' repo")
        url, key_src = _validate_repo_url(rpm_url, "--rpm-url"), rpm_key
    _validate_key_source(key_src, pinned=bool(pins))

    # A same-named repo someone added by hand, or the same URL under another name.
    if family == "apt":
        existing = _apt_entries()
        own_files = {str(_path(_APT_SOURCES_DIR) / f"{name}.list"), str(_path(_APT_SOURCES_DIR) / f"{name}.sources")}
    else:
        existing = _ini_entries(_ZYPP_REPOS if mgr == "zypper" else _YUM_REPOS)
        own_files = {str(_path(_ZYPP_REPOS if mgr == "zypper" else _YUM_REPOS) / f"{name}.repo")}
    warnings: list[str] = []
    for entry in existing:
        if entry["file"] in own_files:
            if not entry.get("managed") and not replace:
                raise LinuxCoreError(f"{entry['file']} already exists and wasn't added by package-repo. Use --replace to overwrite it, or pick another --name.")
        elif _norm_url(url) in {_norm_url(u) for u in entry.get("urls", [])}:
            if family == "apt":
                raise LinuxCoreError(
                    f"{entry['file']} already lists {url}. apt refuses the same repo with two different keys, so remove that one first (package-repo remove {entry['name']}) or keep it and skip this."
                )
            warnings.append(f"{entry['file']} also uses {url}")
    # With --replace, a hand-made <name>.sources would duplicate our <name>.list.
    stale = _path(_APT_SOURCES_DIR) / f"{name}.sources" if family == "apt" and replace else None

    try:
        keys = pgpkey.read_keys(_fetch_key(key_src, proxy=proxy, ca_bundle=ca_bundle, timeout=timeout))
    except pgpkey.KeyFormatError as exc:
        raise LinuxCoreError(f"{key_src} isn't a usable public key: {exc}") from exc
    _check_pins(keys, pins)
    armored = _armored(keys)

    wanted = _apt_files(name, url, apt_suite, components, armored) if family == "apt" else _rpm_files(mgr, name, url, armored, signed_metadata)

    saved = {path: _read(path) for path in wanted}
    if stale is not None and stale.exists():
        saved[stale] = _read(stale)
    key_report = [{"fingerprint": k.fingerprint, "created": time.strftime("%Y-%m-%d", time.gmtime(k.created)), "subkeys": list(k.subkeys)} for k in keys]
    missing_rpm_keys: list[str] = []
    if family == "rpm" and shutil.which("rpm"):
        missing_rpm_keys = [k.rpm_name for k in keys if k.version == 4 and not _rpm_has_key(k.rpm_name)]
    changed = any(saved[path] != data for path, data in wanted.items()) or (stale is not None and saved.get(stale) is not None)
    result = {
        "manager": mgr,
        "name": name,
        "url": url,
        "changed": changed or bool(missing_rpm_keys),
        "refreshed": False,
        "keys": key_report,
        "files": [str(p) for p in wanted],
    }
    if warnings:
        result["warnings"] = warnings
    if not result["changed"]:
        return result

    try:
        for path, data in wanted.items():
            _write(path, data)
        if stale is not None:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        _restore(saved)
        raise LinuxCoreError(f"couldn't write the repo files: {exc}") from exc

    imported: list[str] = []

    def undo() -> None:
        _restore(saved)
        for rpm_name in imported:
            _run(["rpm", "-e", rpm_name])
        if key_changed:
            _forget_repo_keyring(mgr, name)

    key_path = next(iter(wanted))  # the key file comes first
    key_changed = saved.get(key_path) != wanted[key_path]
    for key in keys:
        if key.rpm_name not in missing_rpm_keys:
            continue
        # One key per import: older rpm takes only the first key of a file.
        run = _rpm_import(pgpkey.armor(key.packets))
        if run.returncode != 0:
            undo()
            raise CommandError(
                f"rpm --import refused key {key.fingerprint} (change undone): {run.stderr.strip() or run.stdout.strip()}. If the vendor publishes a separate key for rpm, use that one as --rpm-key."
            )
        imported.append(key.rpm_name)
    if imported:
        result["rpm_keys_imported"] = imported
    if key_changed:
        # dnf/yum keep a per-repo copy of the keys they accepted; drop it so the
        # refresh checks the new key file, not the old copy.
        _forget_repo_keyring(mgr, name)

    if refresh:
        run = _refresh(mgr, name)
        if _refresh_failed(mgr, run):
            undo()
            detail = _tail(run.stderr + run.stdout, 15)
            raise CommandError(f"refreshing the '{name}' repo failed, so the change was undone:\n" + "\n".join(detail))
        result["refreshed"] = True
        result["output"] = _tail(run.stdout, 15)
    return result


# ── removing ──────────────────────────────────────────────────────────────────


def _key_paths_in(entry: dict, key_dir: Path) -> list[Path]:
    refs = [entry.get("signed_by", "")] if "signed_by" in entry else [g.removeprefix("file://") for g in entry.get("gpgkey", [])]
    out = []
    for ref in refs:
        if not ref or not ref.startswith("/"):
            continue
        local = Path(_ROOT) / ref.lstrip("/")
        if local.parent == key_dir:
            out.append(local)
    return out


def remove_repo(name: str) -> dict:
    """Remove a package repository by name (its file name without extension). Needs root.

    Deletes the repo file (apt ``<name>.list``/``.sources``; rpm
    ``<name>.repo``), whether or not ``add_repo`` made it, and the key file it
    points at when that sits in the usual key folder (``/etc/apt/keyrings`` or
    ``/etc/pki/rpm-gpg``) and no other repo uses it. Keys already imported into
    the rpm database stay (another repo may rely on them). Removing a repo that
    isn't there is not an error: ``removed`` is false. Returns
    ``{manager, name, removed, files}``.
    """
    validate_name(name)
    mgr = detect_manager()
    family = _family(mgr)
    if family == "apt":
        entries = _apt_entries()
        candidates = [_path(_APT_SOURCES_DIR) / f"{name}.list", _path(_APT_SOURCES_DIR) / f"{name}.sources"]
        key_dir = _path(_APT_KEYRINGS)
    else:
        entries = _ini_entries(_ZYPP_REPOS if mgr == "zypper" else _YUM_REPOS)
        candidates = [_path(_ZYPP_REPOS if mgr == "zypper" else _YUM_REPOS) / f"{name}.repo"]
        key_dir = _path(_RPM_KEYS)

    targets = [p for p in candidates if p.is_file()]
    target_strs = {str(p) for p in targets}
    keys: set[Path] = set()
    still_used: set[Path] = set()
    for entry in entries:
        paths = _key_paths_in(entry, key_dir)
        if entry["file"] in target_strs:
            keys.update(paths)
        else:
            still_used.update(paths)
    removed: list[str] = []
    try:
        for path in targets + sorted(keys - still_used):
            if path.exists():
                path.unlink()
                removed.append(str(path))
    except OSError as exc:
        raise LinuxCoreError(f"couldn't remove {exc.filename}: {exc.strerror}") from exc
    result = {"manager": mgr, "name": name, "removed": bool(targets), "files": removed}
    if family == "rpm" and targets:
        result["note"] = "keys already imported into the rpm database were kept"
    return result
