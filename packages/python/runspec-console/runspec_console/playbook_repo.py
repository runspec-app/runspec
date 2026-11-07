"""
playbook_repo.py — send a Repos-tab git repo with a ``run-playbook`` run.

``run-playbook`` (runspec-playbook ≥ 0.2.0) runs an Ansible playbook locally on
the host the console reached (``connection: local`` — Ansible opens no SSH of
its own). Its ``--repo`` arg carries ``meta = { console = "playbook-repo" }``,
which tells the console: *this arg names a repo from the Repos tab*. When a run
sets it, the console packs that repo's checkout and sends it with the run:

* ``RUNSPEC_PLAYBOOK_SOURCE = "stdin:<length>:<sha256>"`` rides the env channel
  (the SPEC env frame on stdin for a remote run), and
* ``<length>`` bytes of payload follow the frame on the child's stdin. The env
  prelude reads the frame byte by byte and stops at its end, so the payload is
  still unread when the runnable starts. Stdin passes through sudo / su, so
  ``run_as`` needs nothing extra, nothing is copied to the host first, and
  nothing is left behind.

Payload: ``base64(gzip(json))`` with ``{"v": 1, "repo", "commit", "dirty",
"host", "groups", "files": {path: base64}}``. ``<sha256>`` is
:func:`content_hash` of ``files`` — byte-for-byte the same function as
``runspec_playbook.source.content_hash`` (a shared test vector holds them
together).

**What gets sent.** Interactive runs (Forms, chat, a saved run started by the
operator, an MCP client) send the checkout *as it is*: tracked files plus new
ones, uncommitted edits included, ``.gitignore``d files left out — so the agent
can ``--check`` a playbook it just wrote. Unattended runs (schedules, triggers)
send the **committed** tree only (HEAD of the synced ref): a schedule never runs
an edit nobody committed. Either way the result reports the commit and whether
the checkout had edits.

Pure helpers + dulwich reads (lazy import). No bridge state.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repos

META = "playbook-repo"
ENV_VAR = "RUNSPEC_PLAYBOOK_SOURCE"

# A playbook repo is text: playbooks, roles, templates, vars. These caps stop a
# repo full of build output or vendored binaries from being streamed to every
# host on every run. Past them the run is refused with a plain reason.
MAX_FILES = 5_000
MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

# Run provenance (the ``trigger`` a run carries) that means an operator is
# present — they started it, or approved it in this console or their MCP client.
# Anything else (a schedule, an email/webhook trigger, …) is unattended.
INTERACTIVE_TRIGGERS = frozenset({"", "saved_run", "mcp", "paste", "manual"})


class PlaybookRepoError(ValueError):
    """The repo can't be sent (unknown, not checked out, too big, …)."""


@dataclass(frozen=True)
class Packed:
    """A packed repo, ready to send."""

    env: dict[str, str]  # {ENV_VAR: "stdin:<len>:<sha>"}
    payload: bytes  # ASCII (base64) — follows the env frame on stdin
    repo: str
    commit: str
    dirty: bool
    committed_only: bool
    files: int
    sha256: str

    def describe(self) -> str:
        """One line for the run output: which repo/version was sent."""
        what = f"repo {self.repo}"
        if self.commit:
            what += f" @ {self.commit[:10]}"
        if self.committed_only:
            what += " (committed version"
            what += ", local edits not sent)" if self.dirty else ")"
        elif self.dirty:
            what += " + uncommitted edits"
        return f"▸ Sending {what}: {self.files} files"


# ── arg detection ─────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    return str(name).replace("-", "_")


def repo_arg(record: dict[str, Any] | None, command_path: list[str]) -> str:
    """The name of the arg marked ``meta.console = "playbook-repo"`` on the node
    reached by ``command_path`` (inherited globals included), or ``""``."""
    node: dict[str, Any] = record or {}
    nodes = [node]
    for seg in command_path or []:
        node = (node.get("commands") or {}).get(seg) or {}
        nodes.append(node)
    for n in reversed(nodes):
        for a in n.get("args") or []:
            if not isinstance(a, dict):
                continue
            meta = a.get("meta")
            if isinstance(meta, dict) and meta.get("console") == META and a.get("name"):
                return str(a["name"])
    return ""


def repo_value(args: dict[str, Any], arg_name: str) -> str:
    """The repo name a run's args set for ``arg_name`` (``""`` when unset)."""
    if not arg_name:
        return ""
    for k, v in (args or {}).items():
        if _norm(k) == _norm(arg_name) and v not in (None, ""):
            return str(v).strip()
    return ""


def decorate_records(records: list[dict[str, Any]], repo_names: list[str]) -> int:
    """Offer the registered repos as choices on every playbook-repo arg (in
    place): Forms renders a picker, the agent's schema gets an enum, and the
    preflight bounces a misspelt name. No repos ⇒ a plain str (idempotent, so
    it can re-run over the cache when the Repos list changes). Returns how many
    args it touched."""
    touched = 0

    def walk(node: dict[str, Any]) -> None:
        nonlocal touched
        for a in node.get("args") or []:
            if not isinstance(a, dict):
                continue
            meta = a.get("meta")
            if isinstance(meta, dict) and meta.get("console") == META:
                touched += 1
                a.pop("pattern", None)
                if repo_names:
                    a["type"] = "choice"
                    a["options"] = list(repo_names)
                else:
                    a["type"] = "str"
                    a.pop("options", None)
        for child in (node.get("commands") or {}).values():
            if isinstance(child, dict):
                walk(child)

    for r in records:
        if isinstance(r, dict):
            walk(r)
    return touched


def registered_names() -> list[str]:
    """The Repos-tab repo names ([] when none or unreadable)."""
    try:
        return [r["name"] for r in repos.read_repos()]
    except Exception:  # noqa: BLE001 — a bad registry never breaks discovery
        return []


# ── packing ───────────────────────────────────────────────────────────────────


def content_hash(files_b64: dict[str, str]) -> str:
    """SHA-256 of the sent files — canonical JSON of the path → base64 map.
    Must match ``runspec_playbook.source.content_hash`` exactly."""
    canon = json.dumps(files_b64, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def encode(
    files: dict[str, bytes],
    *,
    repo: str,
    commit: str = "",
    dirty: bool = False,
    host: str = "",
    groups: list[str] | None = None,
    committed_only: bool = False,
) -> Packed:
    """Pack ``files`` into the wire format (pure)."""
    if not files:
        raise PlaybookRepoError(f"repo {repo!r} has no files to send")
    if len(files) > MAX_FILES:
        raise PlaybookRepoError(
            f"repo {repo!r} has {len(files)} files — more than the {MAX_FILES} a "
            "playbook run sends. Keep playbooks in a smaller repo."
        )
    raw = sum(len(b) for b in files.values())
    if raw > MAX_RAW_BYTES:
        raise PlaybookRepoError(
            f"repo {repo!r} is {raw // (1024 * 1024)} MB — more than the "
            f"{MAX_RAW_BYTES // (1024 * 1024)} MB a playbook run sends. Keep large "
            "files out of the playbook repo (copy them with a saved transfer)."
        )
    files_b64 = {
        p: base64.b64encode(b).decode("ascii") for p, b in sorted(files.items())
    }
    sha = content_hash(files_b64)
    doc = {
        "v": 1,
        "repo": repo,
        "commit": commit,
        "dirty": dirty,
        "host": host,
        "groups": list(groups or []),
        "files": files_b64,
    }
    payload = base64.b64encode(
        gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8"), 6)
    )
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PlaybookRepoError(
            f"repo {repo!r} packs to {len(payload) // (1024 * 1024)} MB — more than "
            f"the {MAX_PAYLOAD_BYTES // (1024 * 1024)} MB a playbook run sends."
        )
    return Packed(
        env={ENV_VAR: f"stdin:{len(payload)}:{sha}"},
        payload=payload,
        repo=repo,
        commit=commit,
        dirty=dirty,
        committed_only=committed_only,
        files=len(files),
        sha256=sha,
    )


def _ignore_filter(dest: Path) -> Any:
    """dulwich's .gitignore matcher for the checkout, or None."""
    try:
        from dulwich.ignore import IgnoreFilterManager
        from dulwich.repo import Repo

        repo = Repo(str(dest))
        try:
            return IgnoreFilterManager.from_repo(repo)
        finally:
            repo.close()
    except Exception:  # noqa: BLE001 — no matcher ⇒ send untracked files too
        return None


def checkout_files(dest: Path) -> tuple[dict[str, bytes], dict[str, bytes], str]:
    """``(working, committed, commit)`` for a checkout.

    ``working`` = the working tree minus ``.git``/noise dirs and ``.gitignore``d
    paths (a tracked file that's ignored is still kept); ``committed`` = HEAD's
    tree; ``commit`` = HEAD's full sha."""
    from dulwich.repo import Repo

    repo = Repo(str(dest))
    try:
        commit = repo.head().decode("ascii")
        committed = repos._head_blobs(repo)
    finally:
        repo.close()
    ignore = _ignore_filter(dest)
    working: dict[str, bytes] = {}
    for root, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if d not in repos._SKIP_DIRS]
        for fname in names:
            fpath = Path(root) / fname
            rel = fpath.relative_to(dest).as_posix()
            if rel not in committed and ignore is not None:
                try:
                    if ignore.is_ignored(rel):
                        continue
                except Exception:  # noqa: BLE001
                    pass
            if fpath.is_symlink():
                continue  # never follow a link out of the checkout
            try:
                working[rel] = fpath.read_bytes()
            except OSError:
                continue
    return working, committed, commit


def pack_repo(
    name: str,
    *,
    unattended: bool,
    host: str = "",
    groups: list[str] | None = None,
) -> Packed:
    """Pack Repos-tab repo ``name`` for a run on ``host``."""
    entry = repos.find_repo(name)
    if entry is None:
        known = ", ".join(r["name"] for r in repos.read_repos()) or "none"
        raise PlaybookRepoError(
            f"no repo named {name!r} in the Repos tab (registered: {known})"
        )
    if not repos.available():
        raise PlaybookRepoError("git support (dulwich) is not installed")
    if not repos.is_checked_out(entry["name"]):
        raise PlaybookRepoError(
            f"repo {entry['name']!r} isn't checked out on this console yet — Sync it "
            "in Settings → Repos first"
        )
    working, committed, commit = checkout_files(repos.checkout_dir(entry["name"]))
    dirty = working != committed
    files = committed if unattended else working
    return encode(
        files,
        repo=entry["name"],
        commit=commit,
        dirty=dirty,
        host=host,
        groups=groups,
        committed_only=unattended,
    )
