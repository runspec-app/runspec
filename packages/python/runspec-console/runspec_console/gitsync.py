"""
gitsync.py — pull config files from an external git repo, by tag.

This is the git layer behind Settings → Config Sync. It fetches a handful of
named files (``config.toml``, ``runspec_hosts.toml`` …) from a remote repo at a
specific **ref** (a tag, for versioned rollouts) and hands the raw bytes back to
the bridge, which validates and writes them into the app dir. It never touches
the filesystem itself and imports nothing from the bridge/UI.

**Engine: dulwich** (pure-Python), so a fresh desktop/Windows install needs no
system ``git`` binary. dulwich is an optional extra
(``pip install runspec-console[git-sync]``) and is imported lazily — the rest of
the console runs fine without it (mirrors ``credentials._keyring()``).

Two transports:
  * **HTTPS** — username + secret (a token / password) passed in-process to
    dulwich's HTTP client, so the secret never lands on a command line or in a
    log.
  * **SSH** — over paramiko (already a core dependency) via a small vendor below,
    so SSH repos work without a system ``ssh`` client either. The key file comes
    from an ``ssh-key`` credential (a per-repo deploy key) when one is selected,
    else the console's configured SSH key; an encrypted key's passphrase rides
    a closure in the vendor.

No working tree is checked out: we fetch the ref, peel it to its commit, and read
the wanted files straight out of the commit's tree.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The config files Config Sync knows how to pull, in a stable order. The
# configured ``files`` list defaults to these; new files added here (or to a
# user's list) sync with no other code change. Only bare filenames are allowed —
# the bridge writes each into the app dir, so a name with a path separator is
# rejected upstream (no writing outside the config dir).
KNOWN_FILES: tuple[str, ...] = (
    "config.toml",
    "runspec_hosts.toml",
    "runspec_groups.toml",
    "runspec_profiles.toml",
    "runspec_triggers.toml",
    "runspec_filters.toml",
    "runspec_credentials.toml",
    "runspec_runbooks.toml",
    "runspec_saved_runs.toml",
    "runspec_sounds.toml",
    "runspec_repos.toml",
    "runspec_self_service.toml",
)


class GitSyncError(Exception):
    """A pull/list failed — carries a human-readable message for the UI."""


@dataclass
class GitAuth:
    """How to authenticate to the repo.

    HTTPS uses ``username`` + ``secret`` (a token or password). SSH uses
    ``key_path`` (and optional ``username``, default ``git``). ``scheme`` is
    informational — the actual transport is derived from the URL.

    ``ca_bundle`` (HTTPS) is a path to a PEM bundle to verify the server against —
    needed for an internal CA or a TLS-inspecting corporate proxy, since dulwich
    defaults to certifi and ignores the OS / git cert stores. ``proxy`` (HTTPS) is
    an HTTP CONNECT proxy URL (``http://proxy:8080``, scheme optional) the pull is
    routed through — mirrors the console's ``[ssh] proxy``.
    """

    scheme: str = "https"  # "https" | "ssh" | "local"
    username: str = ""
    secret: str = ""  # token / password — never logged
    key_path: str = ""  # SSH private key file
    passphrase: str = ""  # SSH: passphrase for an encrypted key file — never logged
    ca_bundle: str = ""  # HTTPS: PEM bundle for cert verification
    proxy: str = ""  # HTTPS: HTTP CONNECT proxy URL
    tls_verify: bool = True  # HTTPS: set False to skip cert verification (insecure)


@dataclass
class FetchResult:
    ref: str
    commit: str
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass
class LayeredFetchResult:
    """One fetch, read at several subpaths (overlay layers, low→high precedence).

    ``layers`` maps each requested subpath to the files found there ({} for a
    subpath that carries none). All layers share the one ``commit`` since they
    come from the same ref/tree."""

    ref: str
    commit: str
    layers: dict[str, dict[str, bytes]] = field(default_factory=dict)


# ── engine availability ─────────────────────────────────────────────────────────


def _dulwich() -> Any | None:
    """The ``dulwich.client`` module, or None when the extra isn't installed."""
    try:
        import dulwich.client  # noqa: F401

        return __import__("dulwich.client", fromlist=["client"])
    except Exception:
        return None


def available() -> bool:
    """True when the git engine (dulwich) can be imported."""
    return _dulwich() is not None


def engine_name() -> str | None:
    """The active engine name for the UI, or None when unavailable."""
    return "dulwich" if available() else None


# ── transport ───────────────────────────────────────────────────────────────────


def _is_ssh_url(url: str) -> bool:
    """True for ``ssh://…`` and the scp-like ``git@host:path`` form."""
    u = url.strip()
    if u.startswith("ssh://"):
        return True
    # scp-like: user@host:path with no scheme and a colon before any slash.
    if "://" not in u and "@" in u and ":" in u.split("@", 1)[1]:
        return True
    return False


def _transport_kwargs(url: str, auth: GitAuth) -> dict[str, Any]:
    """dulwich ``get_transport_and_path`` kwargs for this URL + auth.

    For SSH we also install a paramiko vendor (below) so no system ``ssh`` is
    needed. HTTPS passes username/password in-process. Local/``file://`` URLs
    need nothing.
    """
    if _is_ssh_url(url):
        # A key passphrase can't ride dulwich's SSHVendor signature, so the
        # vendor captures it in a closure here (paramiko decrypts the key file).
        _install_paramiko_vendor(auth.passphrase)
        kwargs: dict[str, Any] = {}
        if auth.key_path:
            kwargs["key_filename"] = os.path.expanduser(auth.key_path)
        if auth.username:
            kwargs["username"] = auth.username
        return kwargs
    scheme = urlsplit(url).scheme
    if scheme in ("http", "https"):
        kwargs = {}
        username = auth.username
        # dulwich attaches the HTTP basic-auth header only when a username is
        # present (it keys on `username is not None`), so a token-only credential
        # — the natural pick for a GitLab/GitHub personal access token, which
        # carries no username — would otherwise send no Authorization at all and
        # the server replies 401. When we have a secret but no username, default
        # to the conventional token username so the secret rides as the password
        # ("oauth2" is accepted by GitLab and GitHub PAT auth). A userpass
        # credential keeps its real username untouched.
        if auth.secret and not username:
            username = "oauth2"
        if username:
            kwargs["username"] = username
        if auth.secret:
            kwargs["password"] = auth.secret
        pool = _https_pool_manager(auth)
        if pool is not None:
            kwargs["pool_manager"] = pool
        return kwargs
    return {}  # local path / file://


def _env_ca_bundle() -> str:
    """A CA bundle path from the usual env vars (corporate setups often set one)."""
    for var in ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "REQUESTS_CA_BUNDLE"):
        val = os.environ.get(var)
        if val:
            return val
    return ""


def _env_proxy() -> str:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return ""


def _https_pool_manager(auth: GitAuth) -> Any | None:
    """Build a urllib3 pool manager for HTTPS when a CA bundle, proxy, or
    verification override is needed.

    Returns None when nothing custom applies — dulwich then uses its own default
    (certifi CA + env-var proxy). When we do build one, we must include the proxy
    ourselves (a custom ``pool_manager`` bypasses dulwich's proxy/env handling).
    Prefer pointing ``ca_bundle`` at the internal/corporate CA PEM;
    ``tls_verify=False`` is an escape hatch that skips verification entirely
    (insecure — only on a trusted network)."""
    ca = os.path.expanduser(auth.ca_bundle) if auth.ca_bundle else _env_ca_bundle()
    proxy = auth.proxy or _env_proxy()
    if auth.tls_verify and not ca and not auth.proxy:
        return None  # nothing custom — let dulwich default (certifi + env proxy)

    import urllib3

    opts: dict[str, Any] = {}
    if not auth.tls_verify:
        opts["cert_reqs"] = "CERT_NONE"
        urllib3.disable_warnings()  # silence the InsecureRequestWarning we opted into
    elif ca:
        opts["ca_certs"] = ca
    if proxy:
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        return urllib3.ProxyManager(proxy, **opts)
    return urllib3.PoolManager(**opts)


def _install_paramiko_vendor(passphrase: str = "") -> None:
    """Point dulwich's SSH transport at our paramiko vendor.

    dulwich 1.x defaults to a subprocess vendor that shells out to the system
    ``ssh``. We override it with a paramiko-backed vendor so SSH repos work with
    no external ``ssh`` client (paramiko is already a core dependency). Best
    effort: if paramiko is missing, leave dulwich's default in place.

    ``passphrase`` decrypts an encrypted key file. dulwich's ``SSHVendor``
    interface has no passphrase parameter, so we bind it into the vendor factory
    here rather than threading it through ``run_command``.
    """
    try:
        import dulwich.client as client
    except Exception:
        return
    try:
        import paramiko  # noqa: F401
    except Exception:
        return
    client.get_ssh_vendor = lambda: _ParamikoSSHVendor(passphrase)


class _ParamikoChannel:
    """File-like adapter over a paramiko exec channel for dulwich's Protocol."""

    def __init__(self, ssh: Any, channel: Any) -> None:
        self._ssh = ssh
        self._channel = channel
        self.channel = channel

    def can_read(self) -> bool:
        return self._channel.recv_ready()

    def write(self, data: bytes) -> None:
        self._channel.sendall(data)

    def read(self, n: int | None = None) -> bytes:
        return self._channel.recv(n or 4096)

    @property
    def stderr(self) -> Any:
        return self._channel.makefile_stderr()

    def close(self) -> None:
        try:
            self._channel.close()
        finally:
            self._ssh.close()


def _ParamikoSSHVendor(passphrase: str = "") -> Any:  # noqa: N802 — vendor factory
    """A dulwich SSHVendor backed by paramiko (constructed lazily).

    ``passphrase`` (captured from ``GitAuth``) decrypts an encrypted key file.
    """
    import paramiko
    from dulwich.client import SSHVendor

    class _Vendor(SSHVendor):
        def run_command(
            self,
            host: str,
            command: bytes,
            username: str | None = None,
            port: int | None = None,
            password: str | None = None,
            key_filename: str | None = None,
            ssh_command: str | None = None,
            protocol_version: int | None = None,
        ) -> _ParamikoChannel:
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect: dict[str, Any] = {"hostname": host, "username": username or "git"}
            if port:
                connect["port"] = port
            if password:
                connect["password"] = password
            if key_filename:
                connect["key_filename"] = key_filename
                if passphrase:
                    connect["passphrase"] = passphrase
            ssh.connect(**connect)
            transport = ssh.get_transport()
            if transport is None:
                ssh.close()
                raise GitSyncError(f"could not open an SSH transport to {host}")
            channel = transport.open_session()
            cmd = command.decode() if isinstance(command, bytes) else command
            channel.exec_command(cmd)
            return _ParamikoChannel(ssh, channel)

    return _Vendor()


# ── public API ──────────────────────────────────────────────────────────────────


def list_tags(url: str, auth: GitAuth) -> list[str]:
    """Tag names advertised by the remote (newest-ish first), for the UI dropdown.

    Uses an ``ls-remote``-style ref listing — no objects are downloaded. Raises
    ``GitSyncError`` on connection/auth failure.
    """
    if not available():
        raise GitSyncError(
            "git engine unavailable — install the 'git-sync' extra "
            "(pip install runspec-console[git-sync])"
        )
    from dulwich.client import get_transport_and_path

    try:
        client, path = get_transport_and_path(url, **_transport_kwargs(url, auth))
        result = client.get_refs(path)
        # Newer dulwich returns an LsRemoteResult (use .refs); older returns a dict.
        refs = getattr(result, "refs", result)
    except GitSyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(_human_error(exc)) from exc

    seen: set[str] = set()
    tags: list[str] = []
    prefix = b"refs/tags/"
    for ref in refs:
        if not ref.startswith(prefix):
            continue
        name = ref[len(prefix) :].decode("utf-8", "replace")
        if name.endswith("^{}"):  # peeled annotated tag — same name
            name = name[:-3]
        if name not in seen:
            seen.add(name)
            tags.append(name)
    tags.sort(reverse=True)
    return tags


def fetch_files(
    url: str,
    ref: str,
    files: list[str] | tuple[str, ...],
    auth: GitAuth,
    subpath: str = "",
) -> FetchResult:
    """Fetch ``ref`` and read ``files`` out of its commit tree.

    ``ref`` may be a tag (preferred), a branch, or a full ref name. Each file is
    looked up under ``subpath`` within the repo; a file that doesn't exist at
    that ref is simply omitted from the result (not an error) so a repo needn't
    carry every file. Raises ``GitSyncError`` on connection/auth failure or when
    ``ref`` can't be resolved.
    """
    repo, commit_sha, tree_sha = _fetch_tree(url, ref, auth)
    out = _read_subpath(repo, tree_sha, files, subpath)
    return FetchResult(ref=ref, commit=commit_sha.decode("ascii"), files=out)


def fetch_layers(
    url: str,
    ref: str,
    files: list[str] | tuple[str, ...],
    auth: GitAuth,
    subpaths: list[str] | tuple[str, ...],
) -> LayeredFetchResult:
    """Fetch ``ref`` **once** and read ``files`` at each of ``subpaths``.

    The shared-base/overlay case: one network fetch, then the same commit tree is
    read at every subpath (a base dir, a team dir, …). Each layer's missing files
    are simply omitted (not an error). Layer order is preserved in the result so
    the caller can fold them low→high. Raises ``GitSyncError`` like ``fetch_files``.
    """
    paths = list(subpaths) or [""]
    repo, commit_sha, tree_sha = _fetch_tree(url, ref, auth)
    layers = {sp: _read_subpath(repo, tree_sha, files, sp) for sp in paths}
    return LayeredFetchResult(ref=ref, commit=commit_sha.decode("ascii"), layers=layers)


# ── helpers ─────────────────────────────────────────────────────────────────────


def _fetch_tree(url: str, ref: str, auth: GitAuth) -> tuple[Any, bytes, bytes]:
    """Fetch ``ref`` into a MemoryRepo; return (repo, commit_sha, tree_sha).

    Resolves the ref, peels annotated tags to their commit, and reads the commit
    tree — the shared front half of ``fetch_files`` / ``fetch_layers``."""
    if not available():
        raise GitSyncError(
            "git engine unavailable — install the 'git-sync' extra "
            "(pip install runspec-console[git-sync])"
        )
    from dulwich.client import get_transport_and_path
    from dulwich.repo import MemoryRepo

    repo = MemoryRepo()
    try:
        client, path = get_transport_and_path(url, **_transport_kwargs(url, auth))
        result = client.fetch(path, repo)
        refs = result.refs or {}
    except GitSyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(_human_error(exc)) from exc

    sha = _resolve_ref(refs, ref)
    if sha is None:
        raise GitSyncError(
            f"ref {ref!r} not found in {url} — check the tag/branch name"
        )

    try:
        commit_sha = _peel_to_commit(repo, sha)
        commit = repo.object_store[commit_sha]
        return repo, commit_sha, commit.tree
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(f"could not read commit for ref {ref!r}: {exc}") from exc


def _read_subpath(
    repo: Any, tree_sha: bytes, files: list[str] | tuple[str, ...], subpath: str
) -> dict[str, bytes]:
    """Read ``files`` under ``subpath`` out of a commit tree (absent → omitted)."""
    from dulwich.object_store import tree_lookup_path

    out: dict[str, bytes] = {}
    base = subpath.strip("/")
    for name in files:
        rel = f"{base}/{name}" if base else name
        try:
            _mode, blob_sha = tree_lookup_path(
                repo.object_store.__getitem__, tree_sha, rel.encode("utf-8")
            )
            out[name] = repo.object_store[blob_sha].data
        except KeyError:
            continue  # absent at this ref — fine
    return out


def _resolve_ref(refs: dict[bytes, bytes], ref: str) -> bytes | None:
    """Find the object sha for ``ref`` among advertised refs.

    Tries tag, then branch, then the literal ref / a raw 40-char sha.
    """
    r = ref.encode("utf-8")
    for cand in (
        b"refs/tags/" + r,
        b"refs/heads/" + r,
        b"refs/remotes/origin/" + r,
        r,
    ):
        sha = refs.get(cand)
        if sha:
            return sha
    # a raw sha that was advertised under some ref
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return r
    return None


def _peel_to_commit(repo: Any, sha: bytes) -> bytes:
    """Follow annotated tag objects down to the commit they point at."""
    obj = repo.object_store[sha]
    while getattr(obj, "type_name", b"") == b"tag":
        sha = obj.object[1]
        obj = repo.object_store[sha]
    return sha


def _human_error(exc: Exception) -> str:
    """A friendlier message for the common dulwich failures."""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "401" in text or "unauthorized" in low or "authentication" in low:
        return (
            "authentication failed — check the credential (username + token) and "
            f"that it can read this repo. ({text})"
        )
    if "404" in text or "not found" in low or "not a git repository" in low:
        return f"repository not found — check the URL. ({text})"
    return f"git operation failed: {text}"
