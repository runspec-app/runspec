"""
bridge_memory_sync.py — **Memory Sync**: share the knowledge wiki through git.

A sibling of Config Sync with its own repo and its own (write) credential, so a
leaked or over-scoped wiki token never reaches config. Two operations, both
operator-driven from the Knowledge tab:

* **Push** (narrow) — this console's *approved* facts (``facts.toml``; never the
  inbox, observed usage or retired facts) are written to one file,
  ``facts/<lane>.toml``, on the operator's own branch ``contrib/<lane>``. Nothing
  else in the repo is touched, it is never a force push, and nothing reaches
  ``main`` until someone merges the branch on the repo — the second review.
* **Pull** — every ``facts/*.toml`` on ``main`` (except this operator's own lane)
  replaces the local ``team/`` directory, which the wiki reads as team facts.

Because each contributor only ever changes their own file, merging any number of
``contrib/*`` branches into ``main`` never conflicts.

Push/pull stamps live in the wiki's machine-local ``state.toml``.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import socket
import time
from typing import Any

from . import config as config_mod
from . import knowledge as kn

logger = logging.getLogger(__name__)

FACTS_DIR = "facts"
CONTRIB_PREFIX = "contrib/"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "user"


class MemorySyncMixin:
    # ── settings ─────────────────────────────────────────────────────────────
    def _memory_sync(self) -> dict[str, Any]:
        """``[memory_sync]`` with defaults filled in and the lane derived."""
        ms = config_mod.read_memory_sync()
        user = str(ms.get("user") or "").strip() or _os_user()
        lane = kn.lane_name(user)
        return {
            "url": str(ms.get("url") or "").strip(),
            "credential_id": str(ms.get("credential_id") or "").strip(),
            "branch": str(ms.get("branch") or "").strip()
            or config_mod.DEFAULT_MEMORY_SYNC_BRANCH,
            "user": str(ms.get("user") or "").strip(),
            "ca_bundle": str(ms.get("ca_bundle") or "").strip(),
            "tls_verify": ms.get("tls_verify", True) is not False,
            "lane": lane,
            "contrib_branch": CONTRIB_PREFIX + lane,
            "path": f"{FACTS_DIR}/{lane}.toml",
        }

    def _memory_sync_auth(self, ms: dict[str, Any]) -> Any:
        return self._sync_auth(  # type: ignore[attr-defined]
            ms["url"],
            ms["credential_id"],
            tls={"ca_bundle": ms["ca_bundle"], "tls_verify": ms["tls_verify"]},
        )

    def _memory_sync_state(self) -> dict[str, Any]:
        raw = self._knowledge_store().state().get("memory_sync")  # type: ignore[attr-defined]
        return dict(raw) if isinstance(raw, dict) else {}

    def _memory_sync_stamp(self, **changes: Any) -> None:
        state = self._memory_sync_state()
        state.update(changes)
        self._knowledge_store().update_state(memory_sync=state)  # type: ignore[attr-defined]

    def get_memory_sync_config(self) -> dict[str, Any]:
        """The Settings → Memory Sync form: saved values plus what they resolve
        to (the lane / branch / file a push would write) and whether the git
        engine is installed."""
        from . import gitsync

        ms = self._memory_sync()
        return {
            "config": {
                k: ms[k]
                for k in (
                    "url",
                    "credential_id",
                    "branch",
                    "user",
                    "ca_bundle",
                    "tls_verify",
                )
            },
            "user_default": _os_user(),
            "lane": ms["lane"],
            "contrib_branch": ms["contrib_branch"],
            "path": ms["path"],
            "engine": gitsync.engine_name(),
        }

    def save_memory_sync_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and write ``[memory_sync]``."""
        payload = dict(payload or {})
        url = str(payload.get("url") or "").strip()
        branch = str(payload.get("branch") or "").strip()
        if branch.startswith(CONTRIB_PREFIX):
            return {
                "ok": False,
                "error": "the team branch can't be a contrib/ branch — use main",
            }
        if url and any(c.isspace() for c in url):
            return {"ok": False, "error": "the repository URL can't contain spaces"}
        try:
            config_mod.write_memory_sync(payload)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not save: {exc}"}
        self._knowledge_changed()  # type: ignore[attr-defined]
        return {"ok": True, **self.get_memory_sync_config()}

    # ── status / test ────────────────────────────────────────────────────────
    def memory_sync_status(self) -> dict[str, Any]:
        """What the Knowledge tab's sync bar shows: whether Memory Sync is set up,
        how many approved facts a push would share and whether they changed since
        the last push, and the last push / pull."""
        from . import gitsync

        ms = self._memory_sync()
        store = self._knowledge_store()  # type: ignore[attr-defined]
        data, count = kn.contribution(store.own_facts())
        state = self._memory_sync_state()
        digest = hashlib.sha256(data).hexdigest()
        return {
            "configured": bool(ms["url"]),
            "engine": gitsync.engine_name(),
            "url": ms["url"],
            "branch": ms["branch"],
            "contrib_branch": ms["contrib_branch"],
            "path": ms["path"],
            "shareable": count,
            "unpushed": digest != state.get("last_push_hash", ""),
            "team_facts": len(store.team_facts()),
            "last_push": state.get("last_push", ""),
            "last_push_commit": state.get("last_push_commit", ""),
            "last_pull": state.get("last_pull", ""),
            "last_pull_commit": state.get("last_pull_commit", ""),
        }

    def memory_sync_test(self) -> dict[str, Any]:
        """Check the repo is reachable with the credential (one ``ls-remote``, no
        objects): whether the team branch and this operator's contrib branch
        exist yet."""
        from . import gitsync

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "set a repository URL first"}
        try:
            refs = gitsync._ls_remote(ms["url"], self._memory_sync_auth(ms))
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not reach the repo: {exc}"}
        heads = {
            r[len(b"refs/heads/") :].decode("utf-8", "replace")
            for r in refs
            if r.startswith(b"refs/heads/")
        }
        return {
            "ok": True,
            "branch_exists": ms["branch"] in heads,
            "contrib_exists": ms["contrib_branch"] in heads,
            "empty": not heads,
        }

    # ── push / pull ──────────────────────────────────────────────────────────
    def memory_sync_push(self) -> dict[str, Any]:
        """Push this console's approved facts to ``contrib/<lane>`` (one file).

        Returns ``{ok, branch, commit, changed, created, facts}`` or
        ``{ok: False, error}``."""
        from . import gitsync

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up — add a repo first"}
        store = self._knowledge_store()  # type: ignore[attr-defined]
        data, count = kn.contribution(store.own_facts())
        author = f"{ms['lane']} <{ms['lane']}@{socket.gethostname()}>"
        try:
            res = gitsync.push_file(
                ms["url"],
                ms["contrib_branch"],
                ms["path"],
                data,
                self._memory_sync_auth(ms),
                base_branch=ms["branch"],
                message=f"wiki: {ms['lane']} shares {count} fact(s)",
                author=author,
            )
        except gitsync.PushConflictError as exc:
            return {
                "ok": False,
                "error": f"{ms['contrib_branch']} changed on the repo while pushing "
                f"— try again. ({exc})",
            }
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync push failed")
            return {"ok": False, "error": f"push failed: {exc}"}
        self._memory_sync_stamp(
            last_push=_now(),
            last_push_commit=res.commit,
            last_push_hash=hashlib.sha256(data).hexdigest(),
        )
        self._knowledge_changed()  # type: ignore[attr-defined]
        return {
            "ok": True,
            "branch": res.branch,
            "commit": res.commit,
            "changed": res.changed,
            "created": res.created,
            "facts": count,
        }

    def memory_sync_pull(self) -> dict[str, Any]:
        """Pull the team's facts from ``main`` into ``team/``.

        Reads every ``facts/*.toml`` except this operator's own lane (those facts
        are already local). A brand-new empty repo pulls nothing and isn't an
        error. Files whose names aren't a valid lane are skipped (and listed). Returns
        ``{ok, commit, files, facts, added, removed, skipped}`` or
        ``{ok: False, error}``."""
        from . import gitsync

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up — add a repo first"}
        try:
            res = gitsync.fetch_dir(
                ms["url"], ms["branch"], self._memory_sync_auth(ms), FACTS_DIR
            )
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync pull failed")
            return {"ok": False, "error": f"pull failed: {exc}"}
        own = f"{ms['lane']}.toml"
        files = {n: d for n, d in res.files.items() if n != own and kn.is_team_file(n)}
        skipped = sorted(n for n in res.files if n != own and n not in files)
        try:
            counts = self._knowledge_store().write_team(files)  # type: ignore[attr-defined]
        except ValueError as exc:
            return {"ok": False, "error": f"nothing was pulled: {exc}"}
        self._memory_sync_stamp(last_pull=_now(), last_pull_commit=res.commit)
        self._knowledge_changed()  # type: ignore[attr-defined]
        return {"ok": True, "commit": res.commit, "skipped": skipped, **counts}
