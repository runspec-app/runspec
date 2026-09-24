"""bridge_marketplace.py — MarketplaceMixin: the Marketplace tab's JS API.

The Marketplace lists runspec runnable packages read from package indexes (the
console venv's pip configuration + ``[[marketplace.repos]]``) and installs /
updates / uninstalls them into **venv targets**: the console's own venv and every
``[local] venv_globs`` match, plus each fleet host's ``runspec_paths`` venvs over
the host's pooled SSH connection. The pure logic (index clients, metadata
tiers, pip command builders) lives in :mod:`marketplace`; this seam owns the
I/O — config, the catalogue cache, the fan-out, the streamed progress events —
and nothing else.

Events:

* ``runspec:marketplace_progress`` ``{op, phase, …}`` — catalogue reads
  (``phase: catalog``, per repo) and install/uninstall runs (``phase: start |
  line | done`` per target, with the pip output lines).
* ``runspec:marketplace_done`` ``{op, action, results}`` — one per
  install/uninstall op; the tab re-probes the targets and the console kicks a
  discovery refresh so new runnables surface in Forms / the agent toolset.

Install commands are the same ``<venv>/bin/python -m pip install …`` the
``install-into-venv`` runnable runs, with the same ``sudo -n -u <owner>`` /
``su`` escalation for a service-account venv — but issued directly by the
console (so a host needs no ``runspec-linux`` to be managed), and only ever for
**named package specs** from the configured index (a URL / path / option is
refused by ``marketplace.validate_spec``).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import hosts as hosts_mod
from . import marketplace as mp
from .bridge_const import _AGENT_TOOL_OUTPUT_CAP, logger
from .procutil import no_window_kwargs

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

_PROBE_TIMEOUT_S = 40
# How long a per-target installed-probe row stays fresh. The Marketplace tab
# refreshes on focus/connect/install; within this window a refresh reuses each
# already-probed target's row and re-SSHes only stale or newly-connected ones,
# so the "installed per venv" read is fast and stays accurate. Install/uninstall
# and clear-cache drop the cache so a real change forces a fresh probe.
_PROBE_TTL_S = 60.0
_PIP_TIMEOUT_S = 900
_MAX_OP_LINES = 400  # pip output lines kept per target in the op record
_METADATA_WORKERS = 8
_MARKETPLACE_TOOL_DEFAULT_LIMIT = 15  # search_marketplace results (agent tool)
_MARKETPLACE_TOOL_MAX_LIMIT = 50


class MarketplaceMixin:
    _marketplace_lock: threading.Lock
    _marketplace_ops: dict[str, dict[str, Any]]
    _marketplace_installed: dict[str, Any] | None
    _connected_cache: dict[str, bool]
    _lock: threading.Lock

    # ── JS API ──

    def get_marketplace_config(self) -> dict[str, Any]:
        """The ``[marketplace]`` settings for Settings → Marketplace plus the
        **effective** repo list: the declared ``[[marketplace.repos]]`` and, when
        ``auto_pip`` is on, the indexes derived from this venv's pip config
        (``source: "pip"``; read-only in the UI — edit ``pip.ini`` / declare the
        repo to customise it). Credential ids only, never a secret."""
        cfg = config_mod.read_marketplace()
        repos = self._marketplace_repos(cfg, resolve_auth=False)
        pip_cfg = mp.read_pip_config() if cfg["auto_pip"] else {"sources": []}
        return {
            **{k: v for k, v in cfg.items() if k != "repos"},
            "repos": [r.to_public() for r in repos],
            "declared_repos": [
                r.to_public()
                for r in (mp.repo_from_dict(x) for x in cfg["repos"])
                if r is not None
            ],
            "pip_sources": list(pip_cfg.get("sources") or []),
            "credential_options": self._marketplace_credential_options(),
            "cache_path": str(config_mod.marketplace_cache_path()),
        }

    def save_marketplace_config(self, section: dict[str, Any]) -> dict[str, Any]:
        """Validate + persist the ``[marketplace]`` section (declared repos +
        knobs) and return the fresh :meth:`get_marketplace_config`. Raises
        ``ValueError`` (rejecting the JS promise) on a malformed repo. Removing
        a repo drops its cached catalogue."""
        sec = dict(section or {})
        seen: set[str] = set()
        for raw in sec.get("repos") or []:
            if not isinstance(raw, dict):
                raise ValueError("Each repo must be a table.")
            err = mp.validate_repo(raw)
            if err:
                raise ValueError(err)
            root = mp.simple_root(str(raw.get("url")))
            if root in seen:
                raise ValueError(f"Two repos point at the same index: {root}")
            seen.add(root)
        config_mod.write_marketplace(sec)
        self._invalidate_installed()
        return self.get_marketplace_config()

    def marketplace_catalog(self, refresh: bool = False) -> dict[str, Any]:
        """The catalogue: every runnable package in every enabled repo, one entry
        per package (the first repo in config order wins; ``repos`` lists every
        repo that carries it). Served from the machine-local cache within
        ``cache_ttl``; ``refresh`` re-reads the indexes. A repo that can't be
        read reports its ``error`` and falls back to its last cached entries
        (``stale: true``). Emits ``runspec:marketplace_progress`` per repo."""
        cfg = config_mod.read_marketplace()
        if not cfg["enabled"]:
            return {"enabled": False, "entries": [], "repos": [], "errors": []}
        with self._marketplace_lock:
            return self._read_catalog(cfg, refresh=refresh)

    def marketplace_targets(self) -> list[dict[str, Any]]:
        """The venv targets a package can be installed into — the console's own
        venv + local ``venv_globs`` matches, then each host's ``runspec_paths``
        venvs (the active profile's working set, like the sidebar). Disconnected
        hosts are listed but flagged so the UI can grey them out."""
        return self._marketplace_targets()

    def marketplace_installed(self, refresh: bool = False) -> dict[str, Any]:
        """What each connected target has installed: ``{targets: [{…target,
        ok, error, owner, user, python_version, dists: {name: version}}],
        probed_at}``. ``dists`` is filtered to the marketplace's name patterns,
        the cached catalogue's names and ``runspec`` itself.

        Repeat opens within ``_PROBE_TTL_S`` return the assembled result with no
        work; past that (or on ``refresh``) the probe re-runs but reuses each
        still-fresh target's cached row, so a focus/connect refresh re-SSHes only
        stale or newly-connected targets. Install/uninstall + clear-cache force a
        fresh probe."""
        cached = self._marketplace_installed
        if (
            cached is not None
            and not refresh
            and (time.time() - float(cached.get("probed_at", 0.0))) < _PROBE_TTL_S
        ):
            return cached
        result = self._probe_targets()
        self._marketplace_installed = result
        return result

    def marketplace_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start installing / updating / reinstalling a package on one or more
        targets; returns ``{op}`` immediately and streams
        ``runspec:marketplace_progress`` / ``runspec:marketplace_done``.

        ``payload``: ``package`` (a name, optionally ``name==version``),
        ``targets`` (``[{id, run_as?, become_method?}]``), ``upgrade``,
        ``reinstall``, ``pre``, ``repo`` (the repo id to install from — its
        ``pip_arg`` decides whether ``PIP_INDEX_URL`` / ``PIP_EXTRA_INDEX_URL``
        is set; blank ⇒ the target's own pip config). Raises ``ValueError`` for
        a bad spec / unknown target."""
        return self._start_pip_op("install", payload)

    def marketplace_uninstall(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start uninstalling ``package`` from the given ``targets`` (same
        payload shape and events as :meth:`marketplace_install`)."""
        return self._start_pip_op("uninstall", payload)

    def marketplace_op(self, op: str) -> dict[str, Any] | None:
        """The record of a running / finished install-or-uninstall op (for a
        tab that re-mounts mid-run): ``{op, action, package, status, results}``."""
        rec = self._marketplace_ops.get(str(op))
        return dict(rec) if rec else None

    def marketplace_cancel(self, op: str) -> bool:
        """Best-effort cancel of a running op: targets not yet started are
        skipped and a local pip process is terminated (a remote one-shot SSH
        command runs to completion)."""
        rec = self._marketplace_ops.get(str(op))
        if not rec or rec.get("status") != "running":
            return False
        rec["cancel"].set()
        proc = rec.get("proc")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    def marketplace_clear_cache(self) -> None:
        """Drop the machine-local catalogue cache (the next read re-fetches)."""
        mp.CatalogCache(config_mod.marketplace_cache_path()).clear()
        self._invalidate_installed()

    def _invalidate_installed(self) -> None:
        """Drop the assembled installed result and every per-target probe row, so
        the next read re-probes. Called on install/uninstall, clear-cache, and a
        marketplace-config change (its patterns re-filter which dists are kept)."""
        self._marketplace_installed = None
        with self._marketplace_probe_lock:
            self._marketplace_probe_cache.clear()

    # ── agent tool: read-only marketplace discovery ──

    def _marketplace_agent_tools(self) -> list[dict[str, Any]]:
        """Schema for the read-only ``search_marketplace`` tool — search the
        package indexes' catalogue for INSTALLABLE runnable packages. Offered
        only when ``[marketplace] enabled`` (default true); empty list when
        disabled so a locked-down console drops it (matching the hidden tab and
        the refused install)."""
        if not config_mod.read_marketplace().get("enabled", True):
            return []
        return [
            {
                "name": "search_marketplace",
                "description": (
                    "Search the console's Marketplace catalogue — runspec "
                    "runnable packages available from the operator's configured "
                    "package indexes (PyPI and/or a private Nexus/Artifactory), "
                    "that are INSTALLABLE but not necessarily installed. Use it "
                    "to answer 'is there a runnable package for X I could add?' — "
                    "the complement to search_runnables, which only sees what is "
                    "already installed and runnable now. Returns one entry per "
                    "package: name, latest version, summary, keywords, whether it "
                    "ships a verified runspec.toml, the repo(s) it came from, doc "
                    "and source links, and an 'installed' hint when known. "
                    "Read-only DISCOVERY: you can recommend a package and point "
                    "the operator to the Marketplace tab to install it — you "
                    "cannot install it yourself. Honours the operator's hidden-"
                    "packages list. Served from the machine-local catalogue "
                    "cache (no network unless it's cold)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Text matched case-insensitively against each "
                                "package's name, summary and keywords (e.g. "
                                "'gitlab', 'tls certificate', 'jira'). Omit to "
                                "list the whole catalogue (bounded by limit)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max packages to return (default "
                                f"{_MARKETPLACE_TOOL_DEFAULT_LIMIT}, max "
                                f"{_MARKETPLACE_TOOL_MAX_LIMIT})."
                            ),
                        },
                    },
                },
            }
        ]

    def _run_marketplace_tool(self, ti: dict[str, Any]) -> str:
        """Execute ``search_marketplace`` — read the catalogue (from cache),
        filter by ``query``, and return a compact JSON envelope of installable
        packages. Read-only discovery; the autonomy gate has already let it run.
        Errors come back as ``Error: …`` strings the model can act on."""
        cfg = config_mod.read_marketplace()
        if not cfg.get("enabled", True):
            return "Error: the marketplace is disabled ([marketplace] enabled = false)."
        query = str(ti.get("query") or "").strip().lower()
        try:
            limit = int(ti.get("limit") or _MARKETPLACE_TOOL_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = _MARKETPLACE_TOOL_DEFAULT_LIMIT
        limit = max(1, min(limit, _MARKETPLACE_TOOL_MAX_LIMIT))
        try:
            cat = self.marketplace_catalog(refresh=False)
        except Exception as exc:  # never leak a raw traceback to the model
            logger.debug("search_marketplace failed", exc_info=True)
            return f"Error: could not read the marketplace catalogue: {exc}"
        entries = cat.get("entries") or []

        # Installed hint from the in-memory probe cache only — never trigger a
        # fresh SSH fan-out from an autonomous read.
        installed: set[str] = set()
        probed = self._marketplace_installed
        if isinstance(probed, dict):
            for t in probed.get("targets") or []:
                for name in t.get("dists") or {}:
                    installed.add(mp.normalize_name(str(name)))

        def _match(e: dict[str, Any]) -> bool:
            if not query:
                return True
            hay = " ".join(
                [
                    str(e.get("name") or ""),
                    str(e.get("display_name") or ""),
                    str(e.get("summary") or ""),
                    " ".join(str(k) for k in (e.get("keywords") or [])),
                ]
            ).lower()
            return query in hay

        matched = [e for e in entries if _match(e)]
        results: list[dict[str, Any]] = []
        for e in matched[:limit]:
            links = e.get("links") or {}
            repos = list(e.get("repos") or [])
            if not repos and e.get("repo"):
                repos = [e["repo"]]
            results.append(
                {
                    "name": e.get("name"),
                    "version": e.get("version"),
                    "summary": e.get("summary") or "",
                    "keywords": [str(k) for k in (e.get("keywords") or [])][:8],
                    "verified": bool(e.get("has_runspec_toml")),
                    "repos": repos,
                    "docs": links.get("documentation") or links.get("homepage") or "",
                    "source": links.get("source") or "",
                    "requires_python": e.get("requires_python") or "",
                    "installed": mp.normalize_name(str(e.get("name") or ""))
                    in installed,
                }
            )
        envelope = {
            "enabled": True,
            "count": len(results),
            "total": len(matched),
            "results": results,
            "note": (
                "Installable packages from the operator's configured indexes — "
                "not necessarily installed. Installing is an operator action in "
                "the console's Marketplace tab: recommend a package and point the "
                "operator there; you cannot install it yourself. Use "
                "search_runnables for what's already installed and runnable now."
            ),
        }
        out = json.dumps(envelope, default=str)
        # Trim results (not the note) if a large catalogue overflows the cap.
        while len(out) > _AGENT_TOOL_OUTPUT_CAP and results:
            results.pop()
            envelope["count"] = len(results)
            envelope["truncated"] = True
            out = json.dumps(envelope, default=str)
        return out

    # ── internals: repos + catalogue ──

    def _marketplace_credential_options(self) -> list[dict[str, Any]]:
        opts = getattr(self, "mcp_credential_options", None)
        try:
            return list(opts()) if callable(opts) else []
        except Exception:
            return []

    def _marketplace_repos(
        self, cfg: dict[str, Any], *, resolve_auth: bool
    ) -> list[mp.Repo]:
        """Declared repos merged with the pip-derived ones (declared win), each
        with its credential resolved to Basic auth when asked."""
        declared = [r for r in (mp.repo_from_dict(x) for x in cfg["repos"]) if r]
        for r in declared:
            if not r.patterns and cfg["patterns"] != list(mp.DEFAULT_PATTERNS):
                r.patterns = list(cfg["patterns"])
        pip_repos: list[mp.Repo] = []
        if cfg["auto_pip"]:
            try:
                pip_repos = mp.pip_config_repos()
            except Exception as exc:  # a broken pip.conf never breaks the tab
                logger.warning("marketplace: pip config unreadable: %s", exc)
            for r in pip_repos:
                if cfg["patterns"] != list(mp.DEFAULT_PATTERNS):
                    r.patterns = list(cfg["patterns"])
        repos = mp.merge_repos(declared, pip_repos)
        if resolve_auth:
            for r in repos:
                if r.credential and r.auth is None:
                    r.auth = self._marketplace_auth(r.credential)
        return repos

    def _marketplace_auth(self, cred_id: str) -> tuple[str, str] | None:
        resolver = getattr(self, "_mcp_credential", None)
        if not callable(resolver):
            return None
        try:
            c = resolver(cred_id)
        except Exception as exc:
            logger.warning("marketplace: credential %r unresolvable: %s", cred_id, exc)
            return None
        if not c or not c.get("secret"):
            return None
        user = str(c.get("username") or "")
        if not user and str(c.get("kind") or "") in ("token", "password"):
            user = "__token__"
        return (user, str(c["secret"]))

    def _marketplace_client(self, cfg: dict[str, Any]) -> mp.IndexClient:
        fetch = mp.default_fetch(
            ca_bundle=cfg.get("ca_bundle"),
            tls_verify=bool(cfg.get("tls_verify", True)),
            timeout=float(cfg.get("timeout") or mp.DEFAULT_TIMEOUT_S),
        )
        return mp.IndexClient(
            fetch,
            max_index_bytes=int(cfg["max_index_bytes"]),
            max_wheel_bytes=int(cfg["max_wheel_bytes"]),
            include_prerelease=bool(cfg["include_prerelease"]),
        )

    def _marketplace_progress(self, **detail: Any) -> None:
        dispatch = getattr(self, "_dispatch", None)
        if callable(dispatch):
            try:
                dispatch("runspec:marketplace_progress", detail)
            except Exception:
                pass

    def _read_catalog(self, cfg: dict[str, Any], *, refresh: bool) -> dict[str, Any]:
        cache = mp.CatalogCache(
            config_mod.marketplace_cache_path(), ttl_s=cfg["cache_ttl"]
        )
        repos = [
            r for r in self._marketplace_repos(cfg, resolve_auth=True) if r.enabled
        ]
        hide = cfg.get("hide") or []
        client: mp.IndexClient | None = None
        repo_rows: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        now = time.time()

        def _parallel(
            names: list[str], read: Callable[[str], dict[str, Any]]
        ) -> list[tuple[str, dict[str, Any] | None, Exception | None]]:
            out: list[tuple[str, dict[str, Any] | None, Exception | None]] = []
            if not names:
                return out
            with ThreadPoolExecutor(
                max_workers=min(_METADATA_WORKERS, len(names)),
                thread_name_prefix="mp-meta",
            ) as ex:
                futs = [(n, ex.submit(read, n)) for n in names]
                for n, fut in futs:
                    try:
                        out.append((n, fut.result(), None))
                    except Exception as exc:  # one package never sinks the repo
                        # Carry the exception (not its str) so build_catalog can
                        # tell a scanned empty/typo project (EmptyProjectError —
                        # skipped silently) from a real read failure (warned).
                        out.append((n, None, exc))
            return out

        for repo in repos:
            row: dict[str, Any] = {
                "id": repo.id,
                "name": repo.name,
                "url": repo.simple_url,
                "source": repo.source,
                "scan": repo.scan,
                "packages": list(repo.packages),
                "count": 0,
                "fetched_at": None,
                "stale": False,
                "error": None,
            }
            hit = None if refresh else cache.get(repo.simple_url, now=now)
            entries: list[dict[str, Any]]
            if hit is not None:
                entries = list(hit.get("entries") or [])
                row["fetched_at"] = hit.get("fetched_at")
                repo_errors = list(hit.get("errors") or [])
            else:
                self._marketplace_progress(
                    phase="catalog", repo=repo.id, status="reading"
                )
                if client is None:
                    client = self._marketplace_client(cfg)
                try:
                    entries, repo_errors = mp.build_catalog(
                        client,
                        repo,
                        exclude=cfg["exclude"],
                        require_runspec=bool(cfg["require_runspec"]),
                        parallel=_parallel,
                    )
                    cache.put(repo.simple_url, entries, repo_errors, now=now)
                    row["fetched_at"] = now
                except Exception as exc:
                    row["error"] = str(exc)
                    logger.warning("marketplace: %s unreadable: %s", repo.name, exc)
                    stale = cache.get_stale(repo.simple_url)
                    entries = list((stale or {}).get("entries") or [])
                    repo_errors = list((stale or {}).get("errors") or [])
                    row["stale"] = bool(stale)
                    row["fetched_at"] = (stale or {}).get("fetched_at")
                self._marketplace_progress(
                    phase="catalog",
                    repo=repo.id,
                    status="error" if row["error"] else "done",
                    count=len(entries),
                )
            # Hide curated-out packages (the [marketplace] hide globs) — cached
            # in full so toggling the list takes effect without a re-fetch, but
            # never surfaced (no card, no search, not in the repo count).
            if hide:
                entries = [e for e in entries if not mp.is_hidden(e["name"], hide)]
            row["count"] = len(entries)
            for err in repo_errors:
                errors.append(f"{repo.name}: {err}")
            for e in entries:
                name = e["name"]
                if name in merged:
                    other = merged[name]
                    other.setdefault("repos", []).append(repo.id)
                    # Surface the newest version whichever repo carries it.
                    if (
                        mp.compare_versions(
                            str(e.get("version") or ""), str(other.get("version") or "")
                        )
                        > 0
                    ):
                        other["newest_elsewhere"] = {
                            "repo": repo.id,
                            "version": e.get("version"),
                        }
                    continue
                entry = dict(e)
                entry["repos"] = [repo.id]
                merged[name] = entry
            repo_rows.append(row)
        return {
            "enabled": True,
            "entries": sorted(merged.values(), key=lambda e: e["name"]),
            "repos": repo_rows,
            "errors": errors,
        }

    # ── internals: targets + probe ──

    def _marketplace_targets(self) -> list[dict[str, Any]]:
        entries_fn = getattr(self, "_active_host_entries", None)
        entries = list(entries_fn()) if callable(entries_fn) else []
        out: list[dict[str, Any]] = []
        for h in entries:
            name = str(h.get("name") or "")
            ssh = h.get("ssh")
            with self._lock:
                connected = self._connected_cache.get(name, ssh is None)
            for rp in utils_mod._paths(h):
                root = mp.venv_root(rp)
                out.append(
                    {
                        "id": f"{name}::{root}",
                        "host": name,
                        "venv": hosts_mod.venv_name(rp),
                        "path": root,
                        "python": mp.venv_python(rp),
                        "runspec_path": rp,
                        "local": ssh is None,
                        "connected": bool(connected),
                    }
                )
        return out

    def _target_host_entry(self, host: str) -> dict[str, Any] | None:
        entries_fn = getattr(self, "_active_host_entries", None)
        for h in list(entries_fn()) if callable(entries_fn) else []:
            if str(h.get("name")) == host:
                return h
        all_fn = getattr(self, "_all_host_entries", None)
        for h in list(all_fn()) if callable(all_fn) else []:
            if str(h.get("name")) == host:
                return h
        return None

    def _run_on_target(
        self,
        target: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        run_as: str = "",
        become_method: str = "sudo",
        timeout: int,
        on_line: Callable[[str], None] | None = None,
        proc_sink: Callable[[Any], None] | None = None,
    ) -> tuple[int, str]:
        """Run ``argv`` on a target's host — locally as a subprocess (streaming
        lines when ``on_line`` is given) or over the host's pooled SSH — and
        return ``(exit_code, combined_output)``."""
        if target.get("local"):
            import os

            full_env = dict(os.environ)
            full_env.update(env or {})
            if run_as:
                cmd = mp.remote_command(
                    argv, run_as=run_as, become_method=become_method, env=env
                )
                popen_argv: list[str] | str = cmd
                shell = True
            else:
                popen_argv, shell = list(argv), False
            popen_kwargs: dict[str, Any] = dict(
                shell=shell,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=full_env,
            )
            popen_kwargs.update(no_window_kwargs())
            proc = subprocess.Popen(popen_argv, **popen_kwargs)
            if proc_sink is not None:
                proc_sink(proc)
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                lines.append(line)
                if on_line is not None:
                    on_line(line)
                if time.monotonic() > deadline:
                    proc.kill()
                    lines.append(f"(timed out after {timeout}s)")
                    break
            code = proc.wait()
            return code, "\n".join(lines)
        h = self._target_host_entry(str(target.get("host")))
        if h is None or not h.get("ssh"):
            return -1, f"host {target.get('host')!r} is not configured"
        cmd = mp.remote_command(
            argv, run_as=run_as, become_method=become_method, env=env
        )
        ssh_run = getattr(self, "_ssh_run")
        code, out, err = ssh_run(
            str(h["ssh"]), cmd, identity_file=h.get("identityFile"), timeout=timeout
        )
        text = "\n".join(p for p in ((out or "").rstrip(), (err or "").rstrip()) if p)
        if on_line is not None:
            for line in text.splitlines():
                on_line(line)
        return int(code), text

    def _probe_targets(self) -> dict[str, Any]:
        cfg = config_mod.read_marketplace()
        patterns = [p.lower() for p in cfg["patterns"]]
        cache = mp.CatalogCache(
            config_mod.marketplace_cache_path(), ttl_s=cfg["cache_ttl"]
        )
        known: set[str] = {"runspec"}
        for repo in self._marketplace_repos(cfg, resolve_auth=False):
            stale = cache.get_stale(repo.simple_url)
            for e in (stale or {}).get("entries") or []:
                known.add(str(e.get("name") or ""))
        targets = self._marketplace_targets()
        import fnmatch

        def _keep(name: str) -> bool:
            return name in known or any(fnmatch.fnmatchcase(name, p) for p in patterns)

        now = time.time()

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            row = dict(t)
            row.update(
                {
                    "ok": False,
                    "error": None,
                    "owner": "",
                    "user": "",
                    "python_version": "",
                    "dists": {},
                }
            )
            if not t.get("connected"):
                row["error"] = "host not connected"
                return row
            # Reuse a recent successful probe for this target so a refresh only
            # re-SSHes stale or newly-connected targets. The install state (dists,
            # owner) is carried from the cached row; the live target fields
            # (connected/path/python) come from `t`.
            key = str(t.get("id"))
            with self._marketplace_probe_lock:
                hit = self._marketplace_probe_cache.get(key)
            if hit is not None and (now - hit[0]) < _PROBE_TTL_S and hit[1].get("ok"):
                fresh = dict(hit[1])
                fresh.update(row)  # live target fields win; keep cached probe result
                fresh["dists"] = hit[1].get("dists", {})
                fresh.update(
                    {
                        "ok": True,
                        "error": None,
                        "owner": hit[1].get("owner", ""),
                        "user": hit[1].get("user", ""),
                        "python_version": hit[1].get("python_version", ""),
                    }
                )
                return fresh
            try:
                code, out = self._run_on_target(
                    t, mp.probe_argv(str(t["python"])), timeout=_PROBE_TIMEOUT_S
                )
            except Exception as exc:
                row["error"] = str(exc)
                return row
            data = mp.parse_probe_output(out)
            if data is None:
                row["error"] = (out or "").strip()[-300:] or f"probe exited {code}"
                return row
            row["ok"] = True
            row["owner"] = str(data.get("owner") or "")
            row["user"] = str(data.get("user") or "")
            row["python_version"] = str(data.get("python") or "")
            row["dists"] = {
                n: v for n, v in (data.get("dists") or {}).items() if _keep(n)
            }
            with self._marketplace_probe_lock:
                self._marketplace_probe_cache[key] = (now, dict(row))
            return row

        workers_fn = getattr(self, "_fanout_workers", None)
        try:
            workers = int(workers_fn()) if callable(workers_fn) else 8
        except Exception:
            workers = 8
        rows: list[dict[str, Any]] = []
        if targets:
            with ThreadPoolExecutor(
                max_workers=min(max(1, workers), len(targets)),
                thread_name_prefix="mp-probe",
            ) as ex:
                rows = list(ex.map(_one, targets))
        return {"targets": rows, "probed_at": time.time()}

    # ── internals: install / uninstall ──

    def _start_pip_op(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = config_mod.read_marketplace()
        if not cfg["enabled"]:
            raise ValueError(
                "The marketplace is disabled ([marketplace] enabled = false)."
            )
        payload = dict(payload or {})
        try:
            spec = mp.validate_spec(str(payload.get("package") or ""))
        except mp.MarketplaceError as exc:
            raise ValueError(str(exc)) from exc
        wanted = payload.get("targets") or []
        if not isinstance(wanted, list) or not wanted:
            raise ValueError("Pick at least one target venv.")
        by_id = {t["id"]: t for t in self._marketplace_targets()}
        plan: list[dict[str, Any]] = []
        for w in wanted:
            wid = str(w.get("id") if isinstance(w, dict) else w)
            t = by_id.get(wid)
            if t is None:
                raise ValueError(f"Unknown target {wid!r}.")
            if not t.get("connected"):
                raise ValueError(f"{t['host']} is not connected.")
            run_as = str((w.get("run_as") if isinstance(w, dict) else "") or "").strip()
            if run_as and not re.match(r"^[A-Za-z_][A-Za-z0-9._-]*$", run_as):
                raise ValueError(f"Not a user name: {run_as!r}.")
            become = str(
                (w.get("become_method") if isinstance(w, dict) else "") or "sudo"
            )
            if become not in ("sudo", "su"):
                raise ValueError("become_method must be sudo or su.")
            plan.append({**t, "run_as": run_as, "become_method": become})

        env: dict[str, str] = {}
        repo_id = str(payload.get("repo") or "").strip()
        if action == "install" and repo_id:
            repos = self._marketplace_repos(cfg, resolve_auth=True)
            repo = next((r for r in repos if r.id == repo_id), None)
            if repo is None:
                raise ValueError(f"Unknown repo {repo_id!r}.")
            env = mp.pip_env(repo)

        op = uuid.uuid4().hex[:12]
        record: dict[str, Any] = {
            "op": op,
            "action": action,
            "package": spec,
            "status": "running",
            "started_at": time.time(),
            "results": [],
            "cancel": threading.Event(),
            "proc": None,
        }
        self._marketplace_ops[op] = record
        # Keep only the newest few finished ops around.
        finished = [
            k for k, v in self._marketplace_ops.items() if v.get("status") != "running"
        ]
        for k in finished[:-8]:
            self._marketplace_ops.pop(k, None)

        opts = {
            "upgrade": bool(payload.get("upgrade")),
            "reinstall": bool(payload.get("reinstall")),
            "pre": bool(payload.get("pre") or cfg["include_prerelease"]),
        }
        threading.Thread(
            target=self._run_pip_op,
            args=(record, plan, env, opts),
            name=f"marketplace-{action}",
            daemon=True,
        ).start()
        return {"op": op, "targets": [t["id"] for t in plan]}

    def _run_pip_op(
        self,
        record: dict[str, Any],
        plan: list[dict[str, Any]],
        env: dict[str, str],
        opts: dict[str, Any],
    ) -> None:
        op = record["op"]
        action = record["action"]
        spec = record["package"]
        cancel: threading.Event = record["cancel"]
        results: list[dict[str, Any]] = record["results"]
        lock = threading.Lock()

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            res: dict[str, Any] = {
                "target": t["id"],
                "host": t["host"],
                "venv": t["venv"],
                "ok": False,
                "exit_code": None,
                "summary": "",
                "lines": [],
                "skipped": False,
            }
            if cancel.is_set():
                res["skipped"] = True
                res["summary"] = "cancelled"
                return res
            python = str(t["python"])
            try:
                if action == "install":
                    argv = mp.pip_install_argv(python, [spec], **opts)
                else:
                    argv = mp.pip_uninstall_argv(python, [spec])
            except mp.MarketplaceError as exc:
                res["summary"] = str(exc)
                return res
            self._marketplace_progress(
                op=op,
                phase="start",
                target=t["id"],
                host=t["host"],
                venv=t["venv"],
                action=action,
            )
            lines: list[str] = res["lines"]

            def _line(line: str) -> None:
                if len(lines) < _MAX_OP_LINES:
                    lines.append(line)
                self._marketplace_progress(
                    op=op, phase="line", target=t["id"], line=line
                )

            def _sink(proc: Any) -> None:
                if t.get("local"):
                    record["proc"] = proc

            try:
                code, out = self._run_on_target(
                    t,
                    argv,
                    env=env if action == "install" else None,
                    run_as=str(t.get("run_as") or ""),
                    become_method=str(t.get("become_method") or "sudo"),
                    timeout=_PIP_TIMEOUT_S,
                    on_line=_line,
                    proc_sink=_sink,
                )
            except Exception as exc:
                res["summary"] = str(exc)
                self._marketplace_progress(
                    op=op,
                    phase="done",
                    target=t["id"],
                    ok=False,
                    exit_code=None,
                    summary=str(exc),
                )
                return res
            finally:
                if t.get("local"):
                    record["proc"] = None
            res["exit_code"] = code
            res["ok"] = code == 0
            res["summary"] = mp.summarize_pip_output(out) or f"exit {code}"
            self._marketplace_progress(
                op=op,
                phase="done",
                target=t["id"],
                ok=res["ok"],
                exit_code=code,
                summary=res["summary"],
            )
            return res

        workers_fn = getattr(self, "_fanout_workers", None)
        try:
            workers = int(workers_fn()) if callable(workers_fn) else 4
        except Exception:
            workers = 4
        # Local targets run one at a time (they share the console's CPU and a
        # single cancellable process slot); remote ones fan out.
        local_plan = [t for t in plan if t.get("local")]
        remote_plan = [t for t in plan if not t.get("local")]
        try:
            if remote_plan:
                with ThreadPoolExecutor(
                    max_workers=min(max(1, workers), len(remote_plan)),
                    thread_name_prefix="mp-pip",
                ) as ex:
                    for r in ex.map(_one, remote_plan):
                        with lock:
                            results.append(r)
            for t in local_plan:
                r = _one(t)
                with lock:
                    results.append(r)
        finally:
            record["status"] = "cancelled" if cancel.is_set() else "finished"
            record["finished_at"] = time.time()
            self._invalidate_installed()
            dispatch = getattr(self, "_dispatch", None)
            if callable(dispatch):
                try:
                    dispatch(
                        "runspec:marketplace_done",
                        {
                            "op": op,
                            "action": action,
                            "package": spec,
                            "status": record["status"],
                            "results": [
                                {k: v for k, v in r.items() if k != "lines"}
                                for r in results
                            ],
                        },
                    )
                except Exception:
                    pass
            # New / removed runnables: re-discover so Forms + the agent see them.
            kick = getattr(self, "_kick_refresh", None)
            if callable(kick) and any(r.get("ok") for r in results):
                try:
                    kick(force=True)
                except Exception:
                    pass
