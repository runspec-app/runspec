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
import re
from dataclasses import dataclass, field
from typing import Any, overload
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
    "runspec_evals.toml",
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


def _ls_remote(url: str, auth: GitAuth) -> dict[bytes, bytes]:
    """The remote's advertised refs (``ls-remote``) — no objects downloaded.

    Raises ``GitSyncError`` when the engine is missing or on connection/auth
    failure."""
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
    return dict(refs)


def _tag_names(refs: dict[bytes, bytes]) -> list[str]:
    """Tag names among ``refs`` (peeled ``^{}`` duplicates folded), newest first."""
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
    return sort_tags(tags)


def list_tags(url: str, auth: GitAuth) -> list[str]:
    """Tag names advertised by the remote (newest first), for the UI dropdown.

    Uses an ``ls-remote``-style ref listing — no objects are downloaded. Raises
    ``GitSyncError`` on connection/auth failure.
    """
    return _tag_names(_ls_remote(url, auth))


# ── tag versions (newer-config detection) ─────────────────────────────────────

# A version-like tag: an optional prefix (``v``, ``config-v``, ``emea-``), a
# dotted number, and an optional pre-release suffix (``-rc1``, ``.beta2``).
_TAG_VERSION_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<num>\d+(?:\.\d+)*)(?P<pre>[-.+_]?[A-Za-z][\w.]*)?$"
)


def parse_tag_version(tag: str) -> tuple[str, tuple[int, ...], str] | None:
    """Split ``tag`` into ``(prefix, numbers, pre-release)`` or ``None`` when it
    isn't version-like. ``v1.4.0`` → ``("v", (1, 4, 0), "")``;
    ``emea-v2.0.0-rc1`` → ``("emea-v", (2, 0, 0), "-rc1")``."""
    m = _TAG_VERSION_RE.match(tag.strip())
    if not m:
        return None
    nums = tuple(int(n) for n in m.group("num").split("."))
    return m.group("prefix"), nums, m.group("pre") or ""


def _pad(nums: tuple[int, ...], width: int) -> tuple[int, ...]:
    return nums + (0,) * (width - len(nums))


def sort_tags(tags: list[str]) -> list[str]:
    """Newest first: version-like tags by number (so ``v1.10.0`` sorts above
    ``v1.9.0`` — a plain string sort gets that wrong), a release above its own
    pre-releases; non-version tags after, reverse-alphabetical."""

    def key(tag: str) -> tuple[int, tuple[int, ...], int, str]:
        parsed = parse_tag_version(tag)
        if parsed is None:
            return (0, (), 0, tag)
        _prefix, nums, pre = parsed
        return (1, _pad(nums, 6), 0 if pre else 1, tag)

    return sorted(tags, key=key, reverse=True)


def newer_tag(current: str, tags: list[str]) -> str | None:
    """The newest release tag strictly newer than ``current``, or ``None``.

    Only tags in the **same series** count — the same prefix (so a team on
    ``emea-v1.2.0`` isn't offered ``apac-v1.5.0``) — and pre-release tags
    (``-rc1``) are never offered as an update. ``None`` when ``current`` isn't
    version-like (a branch, or a free-form tag name)."""
    cur = parse_tag_version(current)
    if cur is None:
        return None
    prefix, cur_nums, _pre = cur
    best: tuple[tuple[int, ...], str] | None = None
    for tag in tags:
        parsed = parse_tag_version(tag)
        if parsed is None:
            continue
        t_prefix, nums, pre = parsed
        if t_prefix != prefix or pre:
            continue
        width = max(len(nums), len(cur_nums))
        if _pad(nums, width) <= _pad(cur_nums, width):
            continue
        if best is None or _pad(nums, 6) > _pad(best[0], 6):
            best = (nums, tag)
    return best[1] if best else None


@dataclass
class UpdateCheck:
    """Whether the remote holds newer config than what was last pulled.

    ``kind`` is ``"tag"`` (a newer release tag in the same series —
    ``latest`` names it), ``"branch"`` (the configured branch moved past
    ``last_commit`` — ``latest`` is its new head sha), or ``"none"`` (the ref
    is a non-version tag, so there's nothing to compare)."""

    available: bool
    kind: str
    current: str
    latest: str = ""


def check_update(url: str, ref: str, last_commit: str, auth: GitAuth) -> UpdateCheck:
    """One ``ls-remote`` to answer "is there newer config than ``ref``?".

    A version tag compares against the newest same-series release tag; a branch
    compares its remote head with ``last_commit`` (the commit the last pull
    applied — empty means never pulled, which reports nothing). No objects are
    downloaded. Raises ``GitSyncError`` on connection/auth failure."""
    refs = _ls_remote(url, auth)
    tags = _tag_names(refs)
    ref = ref.strip()
    if ref in tags:
        latest = newer_tag(ref, tags)
        if latest is None and parse_tag_version(ref) is None:
            return UpdateCheck(available=False, kind="none", current=ref)
        return UpdateCheck(
            available=latest is not None, kind="tag", current=ref, latest=latest or ""
        )
    head = refs.get(b"refs/heads/" + ref.encode("utf-8"))
    if head is not None:
        sha = head.decode("ascii", "replace")
        moved = bool(last_commit) and sha != last_commit
        return UpdateCheck(available=moved, kind="branch", current=ref, latest=sha)
    # A version tag that's no longer on the remote (deleted) still has a series.
    latest = newer_tag(ref, tags)
    if latest is not None:
        return UpdateCheck(available=True, kind="tag", current=ref, latest=latest)
    return UpdateCheck(available=False, kind="none", current=ref)


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


# ── Memory Sync: read a directory, push one file ───────────────────────────────
#
# Config Sync only ever pulls. Memory Sync (the team knowledge wiki) also pushes,
# but narrowly: one file, onto the operator's own branch, of a separate repo with
# its own credential. Nothing here can push anywhere else — the caller names the
# branch and the single path, and the push is refused if the branch moved since
# we read it (no force, no overwrite of someone else's work).


@dataclass
class DirFetchResult:
    """The ``*.toml`` blobs directly under one directory of a ref's tree."""

    ref: str
    commit: str
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass
class PushResult:
    """What :func:`push_file` did. ``changed`` is False when the branch already
    held exactly these bytes (no commit was made). ``created`` is True when the
    branch did not exist on the remote before this push."""

    branch: str
    commit: str
    changed: bool
    created: bool = False


class PushConflictError(GitSyncError):
    """The branch moved on the remote between our read and our push."""


def fetch_dir(
    url: str,
    ref: str,
    auth: GitAuth,
    subdir: str,
    suffix: str = ".toml",
    *,
    recursive: bool = False,
) -> DirFetchResult:
    """Fetch ``ref`` and read every ``suffix`` file directly under ``subdir``.

    A missing directory — or a missing ``ref``, as in a brand-new empty repo —
    reads as no files (``commit`` empty for a missing ref). Nested directories are
    ignored unless ``recursive``, which keys files by their path below ``subdir``
    (``"topics/autonomy.md"``). Raises ``GitSyncError`` like :func:`fetch_files`."""
    fetched = _fetch_tree(url, ref, auth, missing_ok=True)
    if fetched is None:
        return DirFetchResult(ref=ref, commit="")
    repo, commit_sha, tree_sha = fetched
    from dulwich.object_store import tree_lookup_path

    out: dict[str, bytes] = {}
    base = subdir.strip("/")
    try:
        dir_sha = tree_sha
        if base:
            _mode, dir_sha = tree_lookup_path(
                repo.object_store.__getitem__, tree_sha, base.encode("utf-8")
            )
        tree = repo.object_store[dir_sha]
    except KeyError:
        return DirFetchResult(ref=ref, commit=commit_sha.decode("ascii"))
    if getattr(tree, "type_name", b"") != b"tree":
        return DirFetchResult(ref=ref, commit=commit_sha.decode("ascii"))

    def walk(node: Any, prefix: str) -> None:
        for entry in node.iteritems():
            name = prefix + entry.path.decode("utf-8", "replace")
            kind = entry.mode & 0o170000
            if kind == 0o040000 and recursive:
                walk(repo.object_store[entry.sha], name + "/")
            elif kind == 0o100000 and name.endswith(suffix):
                out[name] = repo.object_store[entry.sha].data

    walk(tree, "")
    return DirFetchResult(ref=ref, commit=commit_sha.decode("ascii"), files=out)


def push_file(
    url: str,
    branch: str,
    path: str,
    data: bytes,
    auth: GitAuth,
    *,
    base_branch: str = "main",
    message: str = "Update file",
    author: str = "runspec-console <runspec-console@localhost>",
    retries: int = 1,
) -> PushResult:
    """Commit ``data`` at ``path`` onto ``branch`` and push it — one file only.

    The new commit's parent is the branch's current head; a branch that doesn't
    exist yet starts from ``base_branch`` (or from nothing in an empty repo). When
    the file already holds ``data`` nothing is committed. The push is a
    compare-and-swap: if the remote branch moved since we fetched it, we re-read
    and try again (``retries`` times), then raise :class:`PushConflictError` —
    never a force push. Raises ``GitSyncError`` on connection/auth failure or a
    rejected push."""
    return push_files(
        url,
        branch,
        {path: data},
        auth,
        base_branch=base_branch,
        message=message,
        author=author,
        retries=retries,
    )


def push_files(
    url: str,
    branch: str,
    files: dict[str, bytes | None],
    auth: GitAuth,
    *,
    base_branch: str = "main",
    message: str = "Update files",
    author: str = "runspec-console <runspec-console@localhost>",
    retries: int = 1,
    require_new: bool = False,
) -> PushResult:
    """Commit several files onto ``branch`` in one commit and push it.

    ``files`` maps repo paths to their new bytes; ``None`` deletes the path. Same
    rules as :func:`push_file` (parent, compare-and-swap, never a force push, no
    commit when nothing changes). ``require_new`` refuses with ``GitSyncError``
    when ``branch`` already exists — used to seed a fresh repo without ever
    overwriting one that's in use."""
    if not available():
        raise GitSyncError(
            "git engine unavailable — install the 'git-sync' extra "
            "(pip install runspec-console[git-sync])"
        )
    if not files:
        raise GitSyncError("nothing to push")
    changes: dict[str, bytes | None] = {}
    for path, data in files.items():
        rel = path.strip("/")
        if not rel or ".." in rel.split("/"):
            raise GitSyncError(f"refusing to push to path {path!r}")
        changes[rel] = data
    target = b"refs/heads/" + branch.encode("utf-8")
    for attempt in range(retries + 1):
        try:
            return _push_once(
                url,
                target,
                changes,
                auth,
                base_branch,
                message,
                author,
                require_new,
            )
        except PushConflictError:
            if attempt >= retries:
                raise
    raise PushConflictError(f"{branch} kept moving — try again")  # pragma: no cover


def _push_once(
    url: str,
    target: bytes,
    changes: dict[str, bytes | None],
    auth: GitAuth,
    base_branch: str,
    message: str,
    author: str,
    require_new: bool = False,
) -> PushResult:
    import time as _time

    from dulwich.client import get_transport_and_path
    from dulwich.object_store import commit_tree_changes, tree_lookup_path
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.repo import MemoryRepo

    branch = target[len(b"refs/heads/") :].decode("utf-8")
    repo: Any = MemoryRepo()
    try:
        client, rpath = get_transport_and_path(url, **_transport_kwargs(url, auth))
        refs: dict[Any, Any] = dict(client.fetch(rpath, repo).refs or {})
    except GitSyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(_human_error(exc)) from exc

    store: Any = repo.object_store
    head = refs.get(target)
    if require_new and head:
        raise GitSyncError(f"{branch} already exists on the repo")
    parent = head or refs.get(b"refs/heads/" + base_branch.encode("utf-8"))
    tree: Any = Tree()
    if parent:
        parent = _peel_to_commit(repo, parent)
        tree = store[parent].tree
    else:
        store.add_object(tree)
        tree = tree.id

    def current(rel: str) -> bytes | None:
        if not parent:
            return None
        try:
            _mode, sha = tree_lookup_path(store.__getitem__, tree, rel.encode("utf-8"))
        except KeyError:
            return None
        obj = store[sha]
        return obj.data if getattr(obj, "type_name", b"") == b"blob" else None

    edits: list[tuple[bytes, int | None, Any]] = []
    for rel, data in sorted(changes.items()):
        if current(rel) == data:
            continue  # unchanged (or deleting a path that isn't there)
        if data is None:
            edits.append((rel.encode("utf-8"), None, None))
            continue
        blob = Blob.from_string(data)
        store.add_object(blob)
        edits.append((rel.encode("utf-8"), 0o100644, blob.id))
    if not edits and head:
        return PushResult(branch=branch, commit=head.decode("ascii"), changed=False)

    new_tree = commit_tree_changes(store, store[tree], edits)
    commit: Any = Commit()
    commit.tree = new_tree
    commit.parents = [parent] if parent else []
    who = author.encode("utf-8")
    commit.author = commit.committer = who
    now = int(_time.time())
    commit.author_time = commit.commit_time = now
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = message.encode("utf-8") + b"\n"
    store.add_object(commit)

    expected = head

    def update_refs(remote: dict[Any, Any]) -> dict[Any, Any]:
        if remote.get(target) != expected:
            raise PushConflictError(f"{branch} moved on the remote")
        return {target: commit.id}

    try:
        result = client.send_pack(rpath, update_refs, store.generate_pack_data)  # type: ignore[arg-type]
    except GitSyncError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GitSyncError(_human_error(exc)) from exc
    status = getattr(result, "ref_status", None) or {}
    err = status.get(target)
    if err:
        raise GitSyncError(f"push to {branch} rejected: {err}")
    return PushResult(
        branch=branch,
        commit=commit.id.decode("ascii"),
        changed=True,
        created=head is None,
    )


# ── helpers ─────────────────────────────────────────────────────────────────────


@overload
def _fetch_tree(url: str, ref: str, auth: GitAuth) -> tuple[Any, bytes, bytes]: ...
@overload
def _fetch_tree(
    url: str, ref: str, auth: GitAuth, missing_ok: bool
) -> tuple[Any, bytes, bytes] | None: ...


def _fetch_tree(
    url: str, ref: str, auth: GitAuth, missing_ok: bool = False
) -> tuple[Any, bytes, bytes] | None:
    """Fetch ``ref`` into a MemoryRepo; return (repo, commit_sha, tree_sha).

    Resolves the ref, peels annotated tags to their commit, and reads the commit
    tree — the shared front half of ``fetch_files`` / ``fetch_layers``. With
    ``missing_ok`` an unknown ref returns None instead of raising."""
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
    if sha is None and missing_ok:
        return None
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
