"""
repos.py — the local git-repo working layer behind the agent's ``repo_*`` tools.

Where ``gitsync.py`` fetches a *handful of config files* from a remote into memory
(no working tree), this module maintains **on-disk checkouts** of whole repos so
the chat agent can read/grep/edit real source — the "ground the agent in the
actual code" capability. It reuses ``gitsync``'s transport (dulwich + the paramiko
SSH vendor + the HTTPS CA/proxy pool manager) so auth, internal CAs, and the
corporate proxy all work exactly as they do for Config Sync. The bridge builds the
``GitAuth`` (via its ``_sync_auth``) and resolves the registry; this module owns
the git + filesystem mechanics.

Two halves:

* **Registry** (``runspec_repos.toml``) — the controlled list of repos the agent
  may touch (mirrors ``groups.py``/the other collection files; rides Config Sync,
  merged by ``name``). The agent can only act on a repo named here — never an
  arbitrary URL.
* **Working tree** — ``sync_repo`` clones (or fetches + re-checks-out) a registry
  repo into ``{app_dir}/repos/{name}``; the read helpers (``list_dir`` /
  ``read_file`` / ``grep_repo`` / ``diff_repo``) and the gated ``write_file`` then
  operate on that checkout, every path fenced inside it by ``_safe_path``.

**Edits are local only — there is no commit/push here by design.** ``sync_repo``
re-checks-out the ref (rewriting the working tree from the commit), so a re-sync
*discards* uncommitted local edits — same "remote is the source of truth" model as
Config Sync.

The pure helpers (``normalise_repos`` / ``_safe_path`` / ``_is_binary`` /
``_unified_diff``) are duck-typed/string-only and unit-tested on any OS; the
dulwich calls are guarded behind ``available()`` and lazy imports.
"""

from __future__ import annotations

import difflib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import config, gitsync
from .bridge_const import ToolCancelled

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)

# Caps so a pathological repo/file can't blow the model context or wedge a scan.
_READ_MAX_BYTES = 200_000  # a single repo_read is capped at ~200 KB
_GREP_FILE_MAX_BYTES = 2_000_000  # skip files larger than this when scanning
_GREP_MAX_RESULTS = 200  # hard ceiling on grep hits returned
_DIFF_MAX_FILES = 50  # cap files reported in one diff
_DIFF_MAX_LINES = 2_000  # cap total diff lines returned
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache"})


def available() -> bool:
    """True when the git engine (dulwich) is importable — same gate as Config Sync."""
    return gitsync.available()


# ── registry (runspec_repos.toml) ─────────────────────────────────────────────


def load_repos(path: Path) -> list[dict[str, Any]]:
    """The ``[[repo]]`` entries in ``path`` (``[]`` when the file is absent)."""
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("repo", [])


def normalise_repos(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entries with no name/url, de-duplicate by name (case-insensitive, first
    wins, order preserved). Each result is a full ``{name, url, ref, credential_id,
    description}`` dict (``ref`` defaults to ``main``)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in repos or []:
        name = str(r.get("name") or "").strip()
        url = str(r.get("url") or "").strip()
        if not name or not url:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "url": url,
                "ref": str(r.get("ref") or "").strip() or "main",
                "credential_id": str(r.get("credential_id") or "").strip(),
                "description": str(r.get("description") or "").strip(),
            }
        )
    return out


def save_repos(path: Path, repos: list[dict[str, Any]]) -> None:
    """Write the registry as a ``[[repo]]`` array-of-tables (normalised)."""
    lines: list[str] = []
    for r in normalise_repos(repos):
        lines.append("[[repo]]")
        lines.append(f'name = "{r["name"]}"')
        lines.append(f'url = "{r["url"]}"')
        lines.append(f'ref = "{r["ref"]}"')
        if r.get("credential_id"):
            lines.append(f'credential_id = "{r["credential_id"]}"')
        if r.get("description"):
            lines.append(f'description = "{r["description"]}"')
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def read_repos() -> list[dict[str, Any]]:
    """The normalised repo registry from the app dir."""
    return normalise_repos(load_repos(config.repos_path()))


def find_repo(name: str) -> dict[str, Any] | None:
    """The registry entry named ``name`` (case-insensitive), or ``None``."""
    key = str(name or "").strip().casefold()
    for r in read_repos():
        if r["name"].casefold() == key:
            return r
    return None


# ── machine-local checkout-path overrides (runspec_repo_paths.toml) ────────────
# A repo's checkout normally lives in ``{repos_root}/{name}`` (AppData) — local,
# regenerable, never synced. For a repo the operator actively develops, they can
# point the checkout at their own dev folder instead. That choice is
# **machine-local**: stored here (not in the synced registry), because the path is
# specific to this machine's filesystem and must not propagate to other consoles
# via Config Sync. Shape::
#
#     [paths]
#     "payments" = "C:\\dev\\payments"


def load_repo_paths(path: Path) -> dict[str, str]:
    """The ``[paths]`` name→checkout-dir overrides in ``path`` ({} when absent or
    unreadable). Blank values are dropped."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return {}
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return {}
    return {str(k): str(v).strip() for k, v in paths.items() if str(v).strip()}


def save_repo_paths(path: Path, mapping: dict[str, str]) -> None:
    """Write the machine-local checkout-path overrides as a ``[paths]`` table.
    Blank entries are dropped; an empty mapping removes the file entirely so a
    clean install stays clean."""
    clean = {
        str(k).strip(): str(v).strip()
        for k, v in (mapping or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if not clean:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    lines = ["[paths]"]
    for name, p in clean.items():
        lines.append(f"{config._toml_str(name)} = {config._toml_str(p)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_repo_paths() -> dict[str, str]:
    """The machine-local checkout-path overrides from the app dir."""
    return load_repo_paths(config.repo_paths_path())


def custom_path(name: str) -> str:
    """The machine-local custom checkout path for ``name`` (case-insensitive), or
    ``""`` when the repo uses the default AppData location."""
    key = str(name or "").strip().casefold()
    for k, v in read_repo_paths().items():
        if k.casefold() == key:
            return v
    return ""


def set_repo_path(name: str, path: str) -> None:
    """Set (or, with a blank ``path``, clear) the machine-local custom checkout
    location for ``name``. Replaces any existing entry for the name."""
    key = str(name or "").strip().casefold()
    mapping = {k: v for k, v in read_repo_paths().items() if k.casefold() != key}
    p = str(path or "").strip()
    if p:
        mapping[str(name).strip()] = p
    save_repo_paths(config.repo_paths_path(), mapping)


def default_checkout_dir(name: str) -> Path:
    """The default AppData checkout dir (``{repos_root}/{name}``) — always safe to
    delete (regenerable), unlike a custom dev-folder checkout."""
    return config.repos_root() / name


def checkout_dir(name: str) -> Path:
    """The effective local checkout dir for a registry repo. Defaults to the
    AppData ``{repos_root}/{name}``, unless the operator has set a machine-local
    custom checkout path (e.g. their dev folder) for this repo — see
    :func:`read_repo_paths`."""
    custom = custom_path(name)
    if custom:
        return Path(custom).expanduser()
    return default_checkout_dir(name)


def delete_checkout(name: str) -> bool:
    """Delete the repo's **AppData** checkout dir (if any) and drop any
    machine-local path override for the name. A custom dev-folder checkout is
    **never** removed — those are the operator's own working copies, possibly with
    uncommitted work. Returns True when an AppData directory was removed."""
    import shutil

    d = default_checkout_dir(name)
    removed = d.exists()
    if removed:
        shutil.rmtree(d, ignore_errors=True)
    set_repo_path(name, "")  # the registry entry it belonged to is going away
    return removed


def is_checked_out(name: str) -> bool:
    """True when the repo has a **real** local checkout — a ``.git`` dir *and* a
    materialised working tree (at least one non-``.git`` entry). A bare ``.git``
    with no working tree (a checkout that silently failed) reads as not-checked-out,
    so the Settings chip and the agent's read tools don't treat it as ready."""
    d = checkout_dir(name)
    if not (d / ".git").exists():
        return False
    try:
        return any(p.name != ".git" for p in d.iterdir())
    except OSError:
        return False


def head_commit(name: str) -> str:
    """Short HEAD commit sha of the local checkout, or ``""`` when not checked out
    / unreadable. Used by the Settings → Repos tab to show sync state."""
    if not available() or not is_checked_out(name):
        return ""
    try:
        from dulwich.repo import Repo

        repo = Repo(str(checkout_dir(name)))
        try:
            return repo.head().decode("ascii")[:10]
        finally:
            repo.close()
    except Exception:
        return ""


# ── path safety ───────────────────────────────────────────────────────────────


def _safe_path(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and guarantee the result stays inside it.

    Pure/testable. Rejects absolute paths and any ``..`` escape — a crafted path
    from the model (or a repo file) can't read or write outside the checkout.
    Returns the resolved absolute path; raises ``ValueError`` otherwise."""
    rel = str(rel or "").strip().lstrip("/\\")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel!r} escapes the repository")
    return target


# ── sync (clone / fetch + checkout) ───────────────────────────────────────────


def sync_repo(
    url: str,
    ref: str,
    auth: Any,
    dest: Path,
    cancel_event: Any = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Clone ``url`` (or fetch into the existing checkout) and check out ``ref``.

    Reuses ``gitsync``'s transport so SSH/HTTPS/CA/proxy behave exactly like Config
    Sync. The working tree is rewritten to match the ref — so a re-sync **discards**
    any uncommitted local edits (remote = source of truth). Returns
    ``{action, ref, commit, path}``. Raises ``gitsync.GitSyncError`` on failure.

    ``cancel_event`` (anything with ``.is_set()``) and ``on_progress`` (a
    ``str``-callback), when given, make the network fetch interruptible and
    stream the remote's progress: dulwich invokes our ``progress`` callback as
    sideband data arrives, where we emit it and raise ``ToolCancelled`` if a stop
    was requested — aborting the transfer (a half-done fresh clone is removed by
    the cleanup below). Cancellation lands at progress boundaries, so a fetch
    stalled on a silent socket won't interrupt until data flows again."""
    if not available():
        raise gitsync.GitSyncError(
            "git engine unavailable — install the 'git-sync' extra "
            "(pip install runspec-console[git-sync])"
        )
    import shutil

    from dulwich.client import get_transport_and_path
    from dulwich.index import build_index_from_tree
    from dulwich.repo import Repo

    existing = (dest / ".git").exists()
    dest.mkdir(parents=True, exist_ok=True)
    repo = Repo(str(dest)) if existing else Repo.init(str(dest))
    # A fresh clone calls Repo.init, which writes `.git` *before* the network
    # fetch. If anything below fails (auth, transport, bad ref, empty checkout),
    # that orphan `.git` is left behind and would otherwise read as a (misleading)
    # "synced" repo with no files — so on a failed *fresh* clone we remove the
    # whole dir. An update (existing checkout) is left intact.
    try:

        def _progress(data: bytes) -> None:
            # dulwich calls this with the remote's sideband progress text. Use it
            # as our interruption seam (raise to abort the transport) and to
            # surface "Counting/Compressing/Receiving objects…" to the operator.
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelled()
            if on_progress is not None:
                text = data.decode("utf-8", "replace").strip()
                if text:
                    on_progress(text)

        try:
            client, path = get_transport_and_path(
                url, **gitsync._transport_kwargs(url, auth)
            )
            fetch_result = client.fetch(path, repo, progress=_progress)
            refs = fetch_result.refs or {}
        except (gitsync.GitSyncError, ToolCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            raise gitsync.GitSyncError(gitsync._human_error(exc)) from exc

        sha = gitsync._resolve_ref(refs, ref)
        if sha is None:
            raise gitsync.GitSyncError(
                f"ref {ref!r} not found in {url} — check the tag/branch name"
            )
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelled()
            commit_sha = gitsync._peel_to_commit(repo, sha)
            repo.refs[b"HEAD"] = commit_sha  # detached at the synced commit
            # How many entries the commit's tree has — to tell an empty repo
            # (legitimate 0 files) from a checkout that silently wrote nothing.
            tree_entries = len(repo[repo[commit_sha].tree])
            # Write the working tree + index to match the commit (re-sync overwrites
            # tracked files — local edits are discarded; remote is source of truth).
            build_index_from_tree(
                repo.path, repo.index_path(), repo.object_store, repo[commit_sha].tree
            )
        except (gitsync.GitSyncError, ToolCancelled):
            raise
        except Exception as exc:  # noqa: BLE001
            raise gitsync.GitSyncError(
                f"could not check out ref {ref!r}: {exc}"
            ) from exc

        # Verify the working tree actually materialised: a non-empty commit that
        # leaves only .git on disk is a silently-failed checkout (don't report
        # success). Better a clear error than a green "synced" chip over an empty dir.
        try:
            wrote = any(p.name != ".git" for p in dest.iterdir())
        except OSError:
            wrote = False
        if tree_entries > 0 and not wrote:
            raise gitsync.GitSyncError(
                f"fetched {commit_sha.decode('ascii')[:10]} but no files were written "
                f"to the working tree at {dest} (the commit has {tree_entries} "
                "top-level entries) — the checkout did not materialise."
            )
    except Exception:
        repo.close()
        if not existing:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    repo.close()
    return {
        "action": "updated" if existing else "cloned",
        "ref": ref,
        "commit": commit_sha.decode("ascii"),
        "path": str(dest),
    }


# ── read helpers (over the checkout) ──────────────────────────────────────────


def _is_binary(data: bytes) -> bool:
    """Heuristic: a NUL byte in the first chunk ⇒ treat as binary (skip/decline)."""
    return b"\x00" in data[:4096]


def list_dir(dest: Path, rel: str = "") -> list[dict[str, Any]]:
    """List entries directly under ``rel`` in the checkout (dirs first, then files;
    ``.git`` and other noise dirs hidden). Each row: {name, type, size}."""
    target = _safe_path(dest, rel)
    if not target.exists():
        raise FileNotFoundError(f"{rel or '.'} does not exist in this repo")
    if not target.is_dir():
        raise NotADirectoryError(f"{rel} is a file, not a directory")
    rows: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        if entry.is_dir() and entry.name in _SKIP_DIRS:
            continue
        rows.append(
            {
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else 0,
            }
        )
    return rows


def read_file(dest: Path, rel: str, start: int = 0, end: int = 0) -> dict[str, Any]:
    """Read a text file from the checkout, optionally a 1-based line range
    ``[start, end]``. Bounded to ``_READ_MAX_BYTES``. Returns
    {path, lines, total_lines, truncated, content}."""
    target = _safe_path(dest, rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"{rel} is not a file in this repo")
    data = target.read_bytes()
    if _is_binary(data):
        raise ValueError(f"{rel} looks binary — not returning its bytes")
    truncated = len(data) > _READ_MAX_BYTES
    text = data[:_READ_MAX_BYTES].decode("utf-8", "replace")
    all_lines = text.splitlines()
    total = len(all_lines)
    if start or end:
        s = max(1, start or 1)
        e = end or total
        sel = all_lines[s - 1 : e]
        shown = f"{s}-{min(e, total)}"
    else:
        sel = all_lines
        shown = f"1-{total}"
    return {
        "path": rel,
        "lines": shown,
        "total_lines": total,
        "truncated": truncated,
        "content": "\n".join(sel),
    }


def grep_repo(
    dest: Path,
    pattern: str,
    rel: str = "",
    max_results: int = 100,
    cancel_event: Any = None,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Regex-search files under ``rel`` (default whole repo) for ``pattern``.

    Skips ``.git``/noise dirs, binary files, and files over
    ``_GREP_FILE_MAX_BYTES``. Returns up to ``max_results`` (≤ ``_GREP_MAX_RESULTS``)
    rows: {path, line, text}. Raises ``re.error`` on a bad pattern.

    ``cancel_event``/``on_progress`` (when given) make a large scan interruptible
    between files (raising ``ToolCancelled``) and stream a running file/match
    count so the operator can see it is alive."""
    import re

    rx = re.compile(pattern)
    base = _safe_path(dest, rel)
    limit = min(max(1, max_results), _GREP_MAX_RESULTS)
    out: list[dict[str, Any]] = []
    scanned = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in sorted(files):
            if cancel_event is not None and cancel_event.is_set():
                raise ToolCancelled()
            scanned += 1
            if on_progress is not None and scanned % 200 == 0:
                on_progress(f"scanned {scanned} files, {len(out)} matches")
            fpath = Path(root) / fname
            try:
                if fpath.stat().st_size > _GREP_FILE_MAX_BYTES:
                    continue
                data = fpath.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            relpath = fpath.relative_to(dest).as_posix()
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    out.append({"path": relpath, "line": n, "text": line[:400]})
                    if len(out) >= limit:
                        return out
    return out


# ── edit + diff ───────────────────────────────────────────────────────────────


def write_file(dest: Path, rel: str, content: str) -> dict[str, Any]:
    """Create or overwrite a text file in the checkout (parent dirs created).
    Confirm-gated at the tool layer. Returns {path, bytes, created}."""
    target = _safe_path(dest, rel)
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"{rel} is a directory")
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    target.write_bytes(data)
    return {"path": rel, "bytes": len(data), "created": created}


def _head_blobs(repo: Any) -> dict[str, bytes]:
    """Map every path in HEAD's tree → its blob bytes (for diffing the work tree)."""
    from dulwich.object_store import iter_tree_contents

    head = repo[repo.head()]
    out: dict[str, bytes] = {}
    for entry in iter_tree_contents(repo.object_store, head.tree):
        try:
            out[entry.path.decode("utf-8")] = repo.object_store[entry.sha].data
        except KeyError:
            continue
    return out


def _working_files(dest: Path) -> dict[str, bytes]:
    """Map every working-tree path (skipping noise dirs) → its bytes."""
    out: dict[str, bytes] = {}
    for root, dirs, files in os.walk(dest):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            fpath = Path(root) / fname
            try:
                out[fpath.relative_to(dest).as_posix()] = fpath.read_bytes()
            except OSError:
                continue
    return out


def _unified_diff(path: str, before: bytes, after: bytes) -> list[str]:
    """A unified-diff line list for one path, or a one-line note for binary data."""
    if _is_binary(before) or _is_binary(after):
        return [f"Binary file {path} differs"]
    return list(
        difflib.unified_diff(
            before.decode("utf-8", "replace").splitlines(),
            after.decode("utf-8", "replace").splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def diff_repo(dest: Path, rel: str = "") -> dict[str, Any]:
    """Diff the working tree against the synced commit (HEAD).

    Returns {files: [{path, status, diff}], truncated} where ``status`` is
    modified/added/deleted and ``diff`` is a unified-diff string (or a binary
    note). Bounded by ``_DIFF_MAX_FILES`` / ``_DIFF_MAX_LINES``. ``rel`` limits the
    diff to a path prefix. Read-only."""
    if not available():
        raise gitsync.GitSyncError("git engine unavailable")
    from dulwich.repo import Repo

    repo = Repo(str(dest))
    try:
        head = _head_blobs(repo)
    finally:
        repo.close()
    work = _working_files(dest)
    prefix = str(rel or "").strip().strip("/")

    def _wanted(p: str) -> bool:
        return not prefix or p == prefix or p.startswith(prefix + "/")

    paths = sorted({p for p in (set(head) | set(work)) if _wanted(p)})
    files: list[dict[str, Any]] = []
    used_lines = 0
    truncated = False
    for p in paths:
        b, a = head.get(p), work.get(p)
        if b == a:
            continue
        status = (
            "modified"
            if (b is not None and a is not None)
            else ("added" if b is None else "deleted")
        )
        lines = _unified_diff(p, b or b"", a or b"")
        if len(files) >= _DIFF_MAX_FILES or used_lines + len(lines) > _DIFF_MAX_LINES:
            truncated = True
            break
        used_lines += len(lines)
        files.append({"path": p, "status": status, "diff": "\n".join(lines)})
    return {"files": files, "truncated": truncated}
