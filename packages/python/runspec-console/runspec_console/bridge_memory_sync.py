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
  ``pull_from`` can point it at the newest release tag (``v*``) or one pinned
  tag instead, so consoles only see what the team released;
  ``memory_sync_check_update`` reports a newer tag / new commits.

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


def _default_email(lane: str) -> str:
    return f"{lane}@{socket.gethostname()}"


# Words a server uses when it refuses a push over who authored the commit: a
# GitLab push rule ("Author 'x' is not a member of team", "committer email does
# not match"), or a generic pre-receive hook refusal.
_AUTHOR_HINTS = ("author", "committer", "not a member", "pre-receive", "push rule")


def _push_error(text: str) -> str:
    if any(h in text.lower() for h in _AUTHOR_HINTS):
        return (
            f"{text} — the server checks who wrote the commit. Set Commit email in "
            "Settings → Memory Sync to the email on your git account, then push again."
        )
    return text


def _pull_source(ms: dict[str, Any]) -> str:
    """What a pull reads, in words: ``main``, ``the newest v* tag``, ``v1.4.0``."""
    if ms["pull_from"] == "tag":
        return ms["pull_tag"]
    if ms["pull_from"] == "latest-tag":
        return f"the newest {ms['tag_prefix']}* tag"
    return ms["branch"]


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
        pull_from = str(ms.get("pull_from") or "").strip()
        if pull_from not in config_mod.MEMORY_SYNC_PULL_FROM:
            pull_from = "branch"
        return {
            "url": str(ms.get("url") or "").strip(),
            "credential_id": str(ms.get("credential_id") or "").strip(),
            "branch": str(ms.get("branch") or "").strip()
            or config_mod.DEFAULT_MEMORY_SYNC_BRANCH,
            "pull_from": pull_from,
            "pull_tag": str(ms.get("pull_tag") or "").strip(),
            "tag_prefix": str(ms.get("tag_prefix") or "").strip()
            or config_mod.DEFAULT_MEMORY_SYNC_TAG_PREFIX,
            "user": str(ms.get("user") or "").strip(),
            "email": str(ms.get("email") or "").strip(),
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
                    "pull_from",
                    "pull_tag",
                    "tag_prefix",
                    "user",
                    "email",
                    "ca_bundle",
                    "tls_verify",
                )
            },
            "user_default": _os_user(),
            "email_default": _default_email(ms["lane"]),
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
        pull_from = str(payload.get("pull_from") or "branch").strip()
        if pull_from not in config_mod.MEMORY_SYNC_PULL_FROM:
            return {"ok": False, "error": f"unknown pull source {pull_from!r}"}
        if pull_from == "tag" and not str(payload.get("pull_tag") or "").strip():
            return {"ok": False, "error": "name the tag to pull, e.g. v1.0.0"}
        for key in ("pull_tag", "tag_prefix"):
            if any(c.isspace() for c in str(payload.get(key) or "").strip()):
                return {
                    "ok": False,
                    "error": f"the {key.replace('_', ' ')} can't contain spaces",
                }
        if url and any(c.isspace() for c in url):
            return {"ok": False, "error": "the repository URL can't contain spaces"}
        email = str(payload.get("email") or "").strip()
        if email and ("@" not in email or any(c.isspace() or c in "<>" for c in email)):
            return {"ok": False, "error": "the commit email doesn't look like an email"}
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
            "pull_from": ms["pull_from"],
            "pull_source": _pull_source(ms),
            "contrib_branch": ms["contrib_branch"],
            "path": ms["path"],
            "shareable": count,
            "unpushed": digest != state.get("last_push_hash", ""),
            "team_facts": len(store.team_facts()),
            "last_push": state.get("last_push", ""),
            "last_push_commit": state.get("last_push_commit", ""),
            "last_pull": state.get("last_pull", ""),
            "last_pull_commit": state.get("last_pull_commit", ""),
            "last_pull_ref": state.get("last_pull_ref", ""),
        }

    def memory_sync_check_update(self) -> dict[str, Any]:
        """Is there something newer to pull? One ``ls-remote``, no objects.

        ``branch``: the team branch moved past the last pulled commit.
        ``latest-tag``: a newer release tag than the one last pulled (or none
        pulled yet). ``tag``: a newer release in the pinned tag's series —
        pulling won't take it until the pin changes, so ``pinned`` is True.
        Returns ``{ok, available, latest, current, pinned}`` or
        ``{ok: False, error}``."""
        from . import gitsync

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up"}
        state = self._memory_sync_state()
        auth = self._memory_sync_auth(ms)
        try:
            if ms["pull_from"] == "branch":
                chk = gitsync.check_update(
                    ms["url"], ms["branch"], state.get("last_pull_commit", ""), auth
                )
                return {
                    "ok": True,
                    "available": chk.available,
                    "latest": chk.latest,
                    "current": ms["branch"],
                    "pinned": False,
                }
            tags = gitsync.list_tags(ms["url"], auth)
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"could not reach the repo: {exc}"}
        if ms["pull_from"] == "tag":
            newer = gitsync.newer_tag(ms["pull_tag"], tags) or ""
            return {
                "ok": True,
                "available": bool(newer),
                "latest": newer,
                "current": ms["pull_tag"],
                "pinned": True,
            }
        latest = gitsync.latest_release(tags, ms["tag_prefix"]) or ""
        current = state.get("last_pull_ref", "")
        return {
            "ok": True,
            "available": bool(latest) and latest != current,
            "latest": latest,
            "current": current,
            "pinned": False,
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

    def _memory_sync_author(self, ms: dict[str, Any]) -> str:
        name = ms["user"] or _os_user()
        return f"{name} <{ms['email'] or _default_email(ms['lane'])}>"

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
        author = self._memory_sync_author(ms)
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
            return {"ok": False, "error": _push_error(str(exc))}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync push failed")
            return {"ok": False, "error": _push_error(f"push failed: {exc}")}
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
        """Pull the team's facts into ``team/`` from the team branch, the newest
        release tag, or a pinned tag (``pull_from``).

        Reads every ``facts/*.toml`` except this operator's own lane (those facts
        are already local). A brand-new empty repo pulls nothing and isn't an
        error. Files whose names aren't a valid lane are skipped (and listed). Returns
        ``{ok, ref, commit, files, facts, added, removed, skipped}`` or
        ``{ok: False, error}``."""
        from . import gitsync

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up — add a repo first"}
        auth = self._memory_sync_auth(ms)
        try:
            ref = self._memory_sync_pull_ref(ms, auth)
            res = gitsync.fetch_dir(ms["url"], ref, auth, FACTS_DIR)
            if ms["pull_from"] != "branch" and not res.commit:
                return {"ok": False, "error": f"tag {ref} isn't on the repo"}
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
        self._memory_sync_stamp(
            last_pull=_now(), last_pull_commit=res.commit, last_pull_ref=ref
        )
        self._knowledge_changed()  # type: ignore[attr-defined]
        return {
            "ok": True,
            "ref": ref,
            "commit": res.commit,
            "skipped": skipped,
            **counts,
        }

    def _memory_sync_pull_ref(self, ms: dict[str, Any], auth: Any) -> str:
        """The branch or tag a pull reads. ``latest-tag`` asks the repo for its
        tags (one ``ls-remote``) and raises ``GitSyncError`` when there's no
        release tag yet."""
        from . import gitsync

        if ms["pull_from"] == "tag":
            return ms["pull_tag"]
        if ms["pull_from"] == "latest-tag":
            latest = gitsync.latest_release(
                gitsync.list_tags(ms["url"], auth), ms["tag_prefix"]
            )
            if latest is None:
                raise gitsync.GitSyncError(
                    f"the repo has no release tag starting with "
                    f"{ms['tag_prefix']!r} yet — tag the team branch "
                    f"(e.g. {ms['tag_prefix']}1.0.0), or pull from the branch"
                )
            return latest
        return ms["branch"]

    # ── the wiki repo (OKF pages, okf.py) ────────────────────────────────────
    def memory_sync_create_repo(self) -> dict[str, Any]:
        """Seed the Memory Sync repo from the wiki template: the OKF layout,
        README, CI examples, lint and site scripts, and the generated
        ``runspec/`` pages, as one commit on the team branch.

        Into the team branch when it doesn't exist yet (an empty repo). When it
        does (a repo created with a README, or a default branch the host made),
        the template goes to ``setup/wiki-template`` instead, starting from the
        team branch, for a merge request — the team branch itself is never
        written. A team branch that is already a wiki (root ``index.md`` with
        ``okf_version``) is refused: use Refresh runspec pages.
        Returns ``{ok, branch, commit, files, review, base}`` (``review`` true
        when the template went to the setup branch) or ``{ok: False, error}``."""
        from . import gitsync, okf

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up — add a repo first"}
        files = okf.template_files()
        errors = okf.lint_errors(
            {p: d.decode("utf-8") for p, d in files.items() if p.endswith(".md")}
        )
        if errors:  # a broken template is a packaging bug, not the operator's
            return {
                "ok": False,
                "error": "the wiki template fails its own lint: "
                + "; ".join(errors[:3]),
            }
        auth = self._memory_sync_auth(ms)
        target, review = ms["branch"], False
        try:
            try:
                res = gitsync.push_files(
                    ms["url"],
                    ms["branch"],
                    dict(files),
                    auth,
                    base_branch=ms["branch"],
                    message="wiki: start from the runspec-console template",
                    author=self._memory_sync_author(ms),
                    require_new=True,
                )
            except gitsync.GitSyncError as exc:
                if "already exists" not in str(exc) or isinstance(
                    exc, gitsync.PushConflictError
                ):
                    raise
                root = gitsync.fetch_dir(ms["url"], ms["branch"], auth, "", ".md")
                if okf.is_wiki_root(root.files.get("index.md")):
                    return {
                        "ok": False,
                        "error": f"{ms['branch']} is already a wiki, so it wasn't "
                        "touched. Use Refresh runspec pages to update it.",
                    }
                # The team branch exists but isn't a wiki yet (a README, say):
                # put the template on its own branch for a merge request.
                target, review = okf.SETUP_BRANCH, True
                res = gitsync.push_files(
                    ms["url"],
                    target,
                    dict(files),
                    auth,
                    base_branch=ms["branch"],
                    message="wiki: start from the runspec-console template",
                    author=self._memory_sync_author(ms),
                )
        except gitsync.PushConflictError as exc:
            return {
                "ok": False,
                "error": f"the repo changed while creating it — try again. ({exc})",
            }
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": _push_error(str(exc))}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync create repo failed")
            return {"ok": False, "error": _push_error(f"push failed: {exc}")}
        return {
            "ok": True,
            "branch": res.branch,
            "commit": res.commit,
            "files": len(files),
            "review": review,
            "base": ms["branch"],
        }

    def memory_sync_refresh_docs(self) -> dict[str, Any]:
        """Regenerate the wiki's ``runspec/`` pages from this console's
        documentation summaries and push the difference to
        ``refresh/runspec-docs`` for review. Pages whose content didn't change
        are left alone (their date too); pages the summaries no longer produce
        are removed. Returns ``{ok, branch, commit, changed, pages}`` (``pages``
        = pages added, changed or removed) or ``{ok: False, error}``."""
        from . import gitsync, okf

        ms = self._memory_sync()
        if not ms["url"]:
            return {"ok": False, "error": "Memory Sync isn't set up — add a repo first"}
        auth = self._memory_sync_auth(ms)
        try:
            res = gitsync.fetch_dir(
                ms["url"], ms["branch"], auth, okf.GENERATED_DIR, ".md", recursive=True
            )
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync refresh read failed")
            return {"ok": False, "error": f"could not read the wiki: {exc}"}
        if not res.commit:
            return {
                "ok": False,
                "error": f"{ms['branch']} doesn't exist yet — use Create wiki repo first",
            }
        current = {
            f"{okf.GENERATED_DIR}/{name}": data.decode("utf-8", "replace")
            for name, data in res.files.items()
        }
        changes = okf.refresh_changes(current)
        if not changes:
            return {"ok": True, "changed": False, "pages": 0, "branch": ms["branch"]}
        after = dict(current)
        for path, data in changes.items():
            if data is None:
                after.pop(path, None)
            else:
                after[path] = data.decode("utf-8")
        errors = okf.lint_errors(after)
        if errors:
            return {
                "ok": False,
                "error": "the regenerated pages fail lint: " + "; ".join(errors[:3]),
            }
        try:
            pushed = gitsync.push_files(
                ms["url"],
                okf.REFRESH_BRANCH,
                changes,
                auth,
                base_branch=ms["branch"],
                message=f"wiki: refresh runspec pages ({len(changes)} page(s))",
                author=self._memory_sync_author(ms),
            )
        except gitsync.PushConflictError as exc:
            return {
                "ok": False,
                "error": f"{okf.REFRESH_BRANCH} changed while pushing — try again. ({exc})",
            }
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": _push_error(str(exc))}
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory sync refresh failed")
            return {"ok": False, "error": _push_error(f"push failed: {exc}")}
        return {
            "ok": True,
            "changed": pushed.changed,
            "pages": len(changes),
            "branch": pushed.branch,
            "commit": pushed.commit,
        }
