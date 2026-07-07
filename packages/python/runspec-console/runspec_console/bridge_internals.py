"""bridge_internals.py — InternalsMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import hosts as hosts_mod
from .config import (
    filters_path,
)
from .discovery import discover_local, discover_remote
from .hosts import group_members, groups_map, host_in_profile

from .bridge_const import (
    logger,
)
from .bridge_utils import (
    _ensure_putty_proxy_session,
    _parse_ssh,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class InternalsMixin:
    def launch_terminal(self, host: str) -> None:
        """Launch PuTTY in a separate window connected to the host.

        Fire-and-forget: no stdout/stderr capture, no session tracking. PuTTY
        runs as its own top-level process and the user closes it from there.
        """
        import subprocess as _sp

        entry = self._host_entry(host)
        if entry is None:
            raise ValueError(f"Host '{host}' not found")
        ssh = entry.get("ssh")
        if not ssh:
            raise ValueError(
                "Cannot launch terminal to local host — use a remote jump host"
            )
        putty = utils_mod._find_putty_exe("putty.exe")
        if putty is None:
            raise ValueError(
                r"putty.exe not found. Install PuTTY from putty.org — "
                r"expected at C:\Program Files\PuTTY\putty.exe."
            )
        # Parse ssh target for hostname/port; get username from host or global config.
        cfg_ssh = self.get_config().get("ssh", {})
        user, hostname, port = _parse_ssh(ssh)
        if not user:
            user = cfg_ssh.get("user", "")

        cmd = [str(putty)]
        # Route through the HTTP proxy by loading a saved session that carries
        # the proxy config (PuTTY has no proxy CLI flag). Host/user/key below
        # still override the session.
        proxy = cfg_ssh.get("proxy") or ""
        if proxy:
            session = _ensure_putty_proxy_session(proxy)
            if session:
                cmd.extend(["-load", session])
        cmd.append("-ssh")
        if user:
            cmd.extend(["-l", user])
        cmd.append(hostname)
        if port:
            cmd.extend(["-P", str(port)])

        # Prefer PPK v2 (works with all PuTTY versions) over raw OpenSSH key.
        idf = cfg_ssh.get("identityFile", "")
        if idf:
            expanded = Path(idf).expanduser()
            ppk = Path(str(expanded) + ".ppk")
            key_file = ppk if ppk.exists() else expanded
            if not key_file.exists():
                raise ValueError(
                    f"SSH key file not found: {idf}\n"
                    "Generate a new key in Settings → SSH."
                )
            cmd.extend(["-i", str(key_file)])
        _sp.Popen(cmd)

    def launch_local_terminal(self) -> None:
        """Open a local terminal window — tries Windows Terminal, then PowerShell, then cmd."""
        import subprocess as _sp

        for exe in ("wt.exe", "powershell.exe", "cmd.exe"):
            try:
                _sp.Popen([exe])
                return
            except FileNotFoundError:
                continue
        raise ValueError("Could not open a terminal window")

    # ── internals ─────────────────────────────────────────────────────────────

    def _reload_hosts(self) -> None:
        self._hosts = hosts_mod.load_hosts(config_mod.hosts_path())

    def _reload_filters(self) -> None:
        from .filters_store import load_filters

        self._filters = load_filters(filters_path())

    def _filter_entry(self, name: str) -> dict[str, Any] | None:
        """The saved filter named ``name`` (case-sensitive), or None."""
        return next((f for f in self._filters if f.get("name") == name), None)

    def _reload_credentials(self) -> None:
        from .credentials import load_credentials

        self._credentials = load_credentials(config_mod.credentials_path())

    def _credential_entry(self, cred_id: str) -> dict[str, Any] | None:
        """The saved credential metadata for ``cred_id``, or None."""
        return next(
            (c for c in self._credentials if str(c.get("id")) == str(cred_id)), None
        )

    def _resolve_adhoc_credential(
        self, credential_id: Any, user: str | None, password: str | None
    ) -> tuple[str | None, str | None]:
        """Fill an ad-hoc relay's ``(user, password)`` from a saved credential.

        When a ``credential_id`` is given and no explicit ``password`` was typed,
        the credential's stored secret becomes the password and its ``username``
        fills an empty ``user``. A typed password always wins; an unknown id or no
        id leaves the inputs unchanged. The secret is read from the keyring at
        connect time and never stored on the source dict / in_flight / logs.
        """
        if password or not credential_id:
            return user, password
        from . import credentials as cred

        self._reload_credentials()
        entry = self._credential_entry(str(credential_id))
        if entry is None:
            logger.warning("relay credential_id %r not found; ignoring", credential_id)
            return user, password
        if not user and entry.get("username"):
            user = str(entry["username"])
        return user, cred.get_secret(str(credential_id))

    def _credential_env(
        self, host: str, runnable: str, ids: list[str] | None
    ) -> tuple[dict[str, str], list[str]]:
        """Plain, name-derived env vars for the credentials applied to a run.

        ``ids`` selects credentials explicitly (the form's multi-select); when it
        is ``None`` the in-scope set for ``(host, runnable)`` is used (the agent
        path, where credentials auto-apply by scope). Each credential contributes
        ``credentials.env_vars_for`` — e.g. a "Jira" userpass credential sets
        ``JIRA_USERNAME``/``JIRA_PASSWORD``. Secrets are read from the keyring here
        and never surfaced. Returns ``(env, applied_ids)``; on a name collision the
        later credential wins (logged)."""
        from . import credentials as cred

        self._reload_credentials()
        if ids is None:
            entries = list(self._scoped_credentials(host, runnable))
        else:
            entries = []
            for cid in ids:
                entry = self._credential_entry(str(cid))
                if entry is None:
                    logger.warning("credential_id %r not found; ignoring", cid)
                    continue
                entries.append(entry)

        env: dict[str, str] = {}
        applied: list[str] = []
        for entry in entries:
            cid = str(entry.get("id", ""))
            secret = cred.get_secret(cid) if cid else None
            vars_ = cred.env_vars_for(entry, secret)
            if not vars_:
                continue
            for k, v in vars_.items():
                if k in env and env[k] != v:
                    logger.warning(
                        "credential env var %s set by more than one credential; "
                        "the later one wins",
                        k,
                    )
                env[k] = v
            applied.append(cid)
        return env, applied

    @staticmethod
    def _scope_matches(patterns: Any, value: str) -> bool:
        """True if ``value`` matches any fnmatch pattern (empty list = matches all)."""
        import fnmatch

        pats = [str(p).strip() for p in (patterns or []) if str(p).strip()]
        if not pats:
            return True
        return any(fnmatch.fnmatchcase(value, p) for p in pats)

    def _scoped_credentials(self, host: str, runnable: str) -> list[dict[str, Any]]:
        """In-scope credentials for ``(host, runnable)`` — both scope lists must
        match (an empty scope list matches anything). Reads the cache as-is."""
        return [
            c
            for c in self._credentials
            if self._scope_matches(c.get("scope_hosts"), host)
            and self._scope_matches(c.get("scope_runnables"), runnable)
        ]

    @staticmethod
    def _declared_arg_names(
        cached: dict[str, Any] | None, command_path: list[str]
    ) -> set[str]:
        """All arg names declared at the node reached by ``command_path`` (plus the
        top-level args) — used to avoid injecting a credential into an arg the
        runnable doesn't have."""
        node = cached or {}
        names: set[str] = set()

        def collect(n: dict[str, Any]) -> None:
            for a in n.get("args", []) or []:
                if isinstance(a, dict) and a.get("name"):
                    names.add(a["name"])

        collect(node)
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
            collect(node)
        return names

    def _apply_credentials(
        self,
        host: str,
        runnable: str,
        exec_args: dict[str, Any],
        secrets: dict[str, Any],
        explicit_id: str = "",
        valid_args: set[str] | None = None,
        secret_args: set[str] | None = None,
    ) -> list[str]:
        """Inject bound credential values into ``exec_args`` (username, plain) and
        ``secrets`` (the secret, via the env channel), returning the ids applied.

        Every in-scope credential auto-applies; an explicit ``explicit_id`` also
        applies (even out of scope) and wins any per-arg conflict. A value the
        caller already supplied is never overwritten, and when two in-scope
        credentials bind the same arg with no explicit winner it's ambiguous and
        skipped. ``valid_args`` (when not None) limits *auto* injection to args the
        runnable actually declares — so an in-scope credential never injects a
        username/secret into a runnable that doesn't declare that arg (an
        argument-less runnable's empty set blocks all auto-injection). An
        explicitly named credential still applies regardless.

        ``secret_args`` (the runnable's ``password``-typed arg names) drives a
        fallback: a credential applied here with a stored secret but **no**
        ``secret_arg`` binding fills the runnable's sole still-unset password arg —
        so a credential set up for a relay (secret referenced by id) also works on
        a runnable without extra wiring.
        """
        from . import credentials as cred

        self._reload_credentials()
        explicit = self._credential_entry(explicit_id) if explicit_id else None
        if explicit_id and explicit is None:
            logger.warning("credential_id %r not found; ignoring", explicit_id)
        candidates = list(self._scoped_credentials(host, runnable))
        if explicit is not None and explicit not in candidates:
            candidates.append(explicit)  # named explicitly → applies out of scope
        if not candidates:
            return []

        applied: set[str] = set()

        def inject(arg_field: str, is_secret: bool) -> None:
            # Group the binding credentials by the runnable arg they target.
            by_arg: dict[str, list[dict[str, Any]]] = {}
            for c in candidates:
                arg_name = str(c.get(arg_field, "")).strip()
                if arg_name:
                    by_arg.setdefault(arg_name, []).append(c)
            for arg_name, cs in by_arg.items():
                target = secrets if is_secret else exec_args
                if target.get(arg_name) not in (None, ""):
                    continue  # caller-supplied value wins
                if explicit is not None and explicit in cs:
                    chosen: dict[str, Any] | None = explicit
                elif len(cs) == 1:
                    chosen = cs[0]
                    if valid_args is not None and arg_name not in valid_args:
                        continue  # auto-apply only into a declared arg
                else:
                    logger.warning(
                        "ambiguous credential binding for arg %r on %s/%s; skipping",
                        arg_name,
                        host,
                        runnable,
                    )
                    continue
                cid = str(chosen.get("id", ""))
                if is_secret:
                    val = cred.get_secret(cid)
                    if not val:
                        logger.warning(
                            "credential %r has no stored secret; skipping %r",
                            cid,
                            arg_name,
                        )
                        continue
                    secrets[arg_name] = val
                else:
                    uname = str(chosen.get("username", "")).strip()
                    if not uname:
                        continue
                    exec_args[arg_name] = uname
                applied.add(cid)

        inject("username_arg", is_secret=False)
        inject("secret_arg", is_secret=True)

        # Fallback: an applied credential with a stored secret but no secret_arg
        # binding fills the runnable's sole still-unset password-typed arg, so a
        # credential picked for a run (or set up for a relay) "just works" without
        # also wiring secret_arg. Only when unambiguous: exactly one password arg
        # left unset, and either an explicit credential or a single in-scope
        # secret-bearing one.
        pw_unset = [n for n in (secret_args or ()) if secrets.get(n) in (None, "")]
        if len(pw_unset) == 1:
            arg_name = pw_unset[0]
            chosen_fb: dict[str, Any] | None = explicit
            if chosen_fb is None:
                with_secret = [
                    c for c in candidates if cred.has_secret(str(c.get("id", "")))
                ]
                if len(with_secret) == 1:
                    chosen_fb = with_secret[0]
            if chosen_fb is not None:
                val = cred.get_secret(str(chosen_fb.get("id", "")))
                if val:
                    secrets[arg_name] = val
                    applied.add(str(chosen_fb.get("id", "")))

        return sorted(applied)

    def _apply_selected_credentials(
        self,
        runnable: str,
        exec_args: dict[str, Any],
        secrets: dict[str, Any],
        ids: list[str],
        valid_args: set[str] | None = None,
    ) -> list[str]:
        """Forms run-path arg-binding: fill the runnable's args from the
        credentials the operator *explicitly selected* in the run dialog —
        ``username_arg`` → ``exec_args`` (plain argv), ``secret_arg`` → ``secrets``
        (the ``RUNSPEC_*_ARG_*`` env channel). Returns the ids actually applied.

        This is the symmetry fix that lets a credential work the same on the Forms
        path as for the agent (which uses ``_apply_credentials``). It differs from
        that method deliberately:

        * **No scope auto-apply.** Only the passed ``ids`` are considered — Forms
          stays driven by the operator's conscious selection (the "human chooses"
          half of the Forms/agent split), never by an in-scope credential the
          operator didn't pick.
        * **Explicit bindings only.** Only ``username_arg``/``secret_arg`` are
          honoured (no sole-password fallback), so every credential-supplied field
          corresponds to a binding the run dialog can label "supplied by <cred>" —
          keeping the fill visible rather than silent.

        An operator-typed value always wins (in either ``exec_args`` or
        ``secrets``); only declared args (``valid_args``) are filled. A filled
        ``secret_arg`` is popped from ``exec_args`` so it rides the env channel
        only — never argv, which would otherwise win over the env value."""
        from . import credentials as cred

        self._reload_credentials()
        entries = [
            e for cid in ids if (e := self._credential_entry(str(cid))) is not None
        ]
        if not entries:
            return []
        applied: set[str] = set()

        for entry in entries:
            cid = str(entry.get("id", ""))
            uname_arg = str(entry.get("username_arg", "")).strip()
            if uname_arg and (valid_args is None or uname_arg in valid_args):
                if exec_args.get(uname_arg) in (None, ""):
                    uname = str(entry.get("username", "")).strip()
                    if uname:
                        exec_args[uname_arg] = uname
                        applied.add(cid)
            secret_arg = str(entry.get("secret_arg", "")).strip()
            if secret_arg and (valid_args is None or secret_arg in valid_args):
                already = secrets.get(secret_arg) not in (None, "") or exec_args.get(
                    secret_arg
                ) not in (None, "")
                if not already:
                    val = cred.get_secret(cid)
                    if val:
                        secrets[secret_arg] = val
                        # Never also pass it (empty) on argv — CLI would win over
                        # the env channel and blank the secret.
                        exec_args.pop(secret_arg, None)
                        applied.add(cid)

        return sorted(applied)

    def _local_host_entry(self) -> dict[str, Any]:
        """Synthetic entry for the venv runspec-console itself runs in."""
        scripts = Path(sys.executable).parent
        # Windows venvs use Scripts\runspec.exe; Linux/Mac use bin/runspec
        for name in ("runspec.exe", "runspec"):
            candidate = scripts / name
            if candidate.exists():
                return {"name": "local", "runspec_paths": [str(candidate)], "ssh": None}
        # Fallback: construct the expected path even if not yet installed
        return {
            "name": "local",
            "runspec_paths": [str(scripts / "runspec.exe")],
            "ssh": None,
        }

    def _host_entry(self, host: str) -> dict[str, Any] | None:
        if host == "local":
            return self._local_host_entry()
        return next((h for h in self._hosts if h.get("name") == host), None)

    def _all_host_entries(self) -> list[dict[str, Any]]:
        """The full host set (local first, then the configured remotes) — the
        definition shared by the group resolvers and any caller that must reach
        **every** configured host regardless of the active profile (explicit runs,
        triggers, schedules — automation spans profiles). Reloads from disk so
        callers see the current file. For the interactive working set (sidebar /
        refresh cycle) use :meth:`_active_host_entries`."""
        self._reload_hosts()
        local = self._local_host_entry()
        return [local] + [h for h in self._hosts if h.get("name") != "local"]

    def _active_profile_name(self) -> str:
        """The machine-local active host profile (``""`` = the built-in *All hosts*
        scope, i.e. no filter). See ``docs/design/host-profiles.md``."""
        from . import profiles as profiles_mod

        return profiles_mod.read_active_profile(config_mod.active_profile_path())

    def _active_host_entries(self) -> list[dict[str, Any]]:
        """The **interactive working set**: local + the remotes in the active
        profile. This is what the sidebar (``get_hosts``) shows and what the
        refresh cycle keeps probed/connected — so an unloaded profile's hosts hold
        no standing connection. ``local`` is always in scope. A blank active
        profile (*All hosts*) returns the full set (today's behavior)."""
        self._reload_hosts()
        local = self._local_host_entry()
        active = self._active_profile_name()
        remotes = [h for h in self._hosts if h.get("name") != "local"]
        if active:
            remotes = [h for h in remotes if host_in_profile(h, active)]
        return [local] + remotes

    def _group_members(self, group: str) -> list[str]:
        """Host names in ``group`` (see :func:`hosts.group_members`)."""
        return group_members(self._all_host_entries(), group)

    def _groups_map(self) -> dict[str, list[str]]:
        """``{group: [host names]}`` over the live host set."""
        return groups_map(self._all_host_entries())

    def _host_has_runnable(self, host: str, name: str) -> bool:
        """Whether ``host`` has a cached runnable named ``name`` (used to skip
        group members that don't carry the runnable in a fan-out)."""
        with self._lock:
            return any(
                r.get("host") == host and r.get("name") == name
                for r in self._runnables_cache
            )

    def _start_refresh_watcher(self) -> None:
        """Run a full refresh cycle, then repeat every ``[refresh] interval`` s."""
        from .ssh_pool import refresh_settings

        def _loop() -> None:
            while True:
                self._refresh_cycle()
                interval = refresh_settings(self.get_config().get("refresh"))[
                    "interval"
                ]
                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True).start()

    def _probe_one(self, h: dict[str, Any], *, jitter: float = 0) -> None:
        """One host's connectivity probe (a refresh-cycle worker task): optional
        start jitter to spread connects, then record the result in the cache."""
        if jitter:
            time.sleep(random.uniform(0, jitter))  # spread the connects
        result = self._check_connected(h)
        with self._lock:
            self._connected_cache[h["name"]] = result

    def _discover_one(self, h: dict[str, Any]) -> list[dict[str, Any]]:
        """One host's runnable discovery (a refresh-cycle worker task). Returns the
        discovered runnables (empty if the host was marked disconnected by the
        probe phase), so the caller can flatten results without a shared lock."""
        ssh = h.get("ssh")
        name = h["name"]
        with self._lock:
            connected = self._connected_cache.get(name, ssh is None)
        if not connected:
            return []
        runner = self._ssh_pool.run if self._ssh_pool is not None else None
        idf = h.get("identityFile")
        out: list[dict[str, Any]] = []
        for rp in utils_mod._paths(h):
            out.extend(
                discover_remote(
                    ssh,
                    rp,
                    name,
                    idf,
                    global_ssh_config=self.get_config().get("ssh"),
                    runner=runner,
                )
                if ssh
                else discover_local(rp, name)
            )
        return out

    @staticmethod
    def _dedup_host_runnables(
        runnables: list[dict[str, Any]], host_name: str
    ) -> list[dict[str, Any]]:
        """Drop duplicate ``(host, name)`` runnables within one host's discovery
        (the same runnable name shipped by two installed packages on that host),
        keeping the first and warning. Cross-host collisions can't happen — the
        key includes the host — so this is safe to run per host as it completes."""
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for r in runnables:
            key = (r.get("host", ""), r.get("name", ""))
            if key in seen:
                sys.stderr.write(
                    f"runspec-console: warning: '{key[1]}' defined in multiple"
                    f" installed packages on host '{key[0] or host_name}', keeping first\n"
                )
                sys.stderr.flush()
                continue
            seen.add(key)
            out.append(r)
        return out

    def _refresh_cycle(self) -> None:
        """Probe connectivity, then discover runnables — reusing pooled connections.

        With the pool on, Phase 1 *warms* the per-host transport that Phase 2's
        discovery then reuses, so a host costs one handshake per cycle (often
        zero once warm) instead of the old ``probe + per-path`` handshake storm.
        Per-host jitter spreads the cycle's connects so they don't all fire on
        the same instant; the pool's global semaphore bounds how many connect at
        once; ``reap()`` closes transports gone idle.
        """
        from concurrent.futures import ThreadPoolExecutor

        from .ssh_pool import pool_settings, refresh_settings

        # Only the active profile's hosts are probed and kept warm — an unloaded
        # profile's hosts hold no standing connection (the point of profiles).
        # Automation still reaches out-of-scope hosts on demand via _host_entry /
        # _all_host_entries; the pool idle-reaps those transient transports.
        all_hosts = self._active_host_entries()
        max_concurrent = pool_settings(self.get_config().get("ssh"))["max_concurrent"]
        settings = refresh_settings(
            self.get_config().get("refresh"), max_concurrent=max_concurrent
        )
        jitter = settings["jitter"]
        # Bounded worker count for the fan-out — see refresh_settings. A patched
        # refresh_settings (tests) may omit it; fall back to a safe default.
        workers = max(1, int(settings.get("workers") or 16))

        # ── Phase 1: connectivity (warms the pooled transport) ─────────────────
        # A bounded pool instead of one thread per host: at large fleets this
        # holds a constant ~workers threads rather than ~N. Order is preserved —
        # the whole probe phase completes before hosts_updated fires and before
        # discovery (which skips hosts the probe marked disconnected) runs.
        with self._lock:
            self._connected_cache["local"] = True

        ssh_hosts = [h for h in all_hosts if h.get("ssh")]
        if ssh_hosts:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(ssh_hosts)),
                thread_name_prefix="refresh-probe",
            ) as ex:
                list(ex.map(lambda h: self._probe_one(h, jitter=jitter), ssh_hosts))
        self._dispatch("runspec:hosts_updated", {})

        # ── Phase 2: runnables discovery (reuses the warm transport) ───────────
        # Merge each host's runnables into the cache *as it completes* rather than
        # waiting for the whole fleet, so local (no SSH — near-instant) and fast
        # hosts are usable on the very first turn instead of being blocked behind
        # the slowest bastion. Dedup is per-host (the key includes the host, so two
        # hosts never collide); a removed host is pruned by the canonical assign at
        # the end of the cycle.
        from concurrent.futures import as_completed

        total = len(all_hosts)
        self._dispatch(
            "runspec:discovery_state",
            {"done": 0, "total": total, "ready": self._discovery_ready},
        )
        accumulated: list[dict[str, Any]] = []
        done = 0
        with ThreadPoolExecutor(
            max_workers=min(workers, len(all_hosts)),
            thread_name_prefix="refresh-disc",
        ) as ex:
            futures = {ex.submit(self._discover_one, h): h for h in all_hosts}
            for fut in as_completed(futures):
                host_name = futures[fut].get("name", "")
                try:
                    host_runnables = fut.result()
                except Exception:
                    logger.debug(
                        "discovery failed for host %s", host_name, exc_info=True
                    )
                    host_runnables = []
                cleaned = self._dedup_host_runnables(host_runnables, host_name)
                accumulated.extend(cleaned)
                done += 1
                # Splice this host's results into the live cache: drop its prior
                # entries, keep every other host's, append the fresh set. Never
                # empties the cache mid-cycle (steady-state refreshes stay warm).
                with self._lock:
                    self._runnables_cache = [
                        r
                        for r in self._runnables_cache
                        if r.get("host", "") != host_name
                    ] + cleaned
                    self._discovery_progress = (done, total)
                self._dispatch("runspec:runnables_updated", {})
                self._dispatch(
                    "runspec:discovery_state",
                    {"done": done, "total": total, "ready": self._discovery_ready},
                )

        # Canonical assign: prunes any host that left the fleet (its entries linger
        # in the spliced cache until here), and marks discovery ready after the
        # first full cycle so the readiness indicator clears.
        with self._lock:
            self._runnables_cache = accumulated
            self._discovery_ready = True
            self._discovery_progress = (total, total)
        self._dispatch("runspec:runnables_updated", {})
        self._dispatch(
            "runspec:discovery_state", {"done": total, "total": total, "ready": True}
        )
        # The fleet's runnables changed — refresh every staffed queue's
        # self-service catalog so visitors are offered what can run *now*.
        try:
            self._publish_catalogs()
        except Exception:
            logger.exception("chat: catalog republish after discovery failed")

        # Close transports that have gone idle beyond their TTL (cheap, per-cycle
        # — no separate timer thread).
        if self._ssh_pool is not None:
            self._ssh_pool.reap()

    def _check_connected(self, host: dict[str, Any]) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True
        pool = self._ssh_pool
        if pool is not None:
            # Probe-free: obtaining a live pooled transport IS the connectivity
            # check, and it warms the connection discovery reuses this cycle —
            # no separate ``ssh host true`` handshake.
            return pool.connectable(
                ssh,
                identity_file=host.get("identityFile"),
                global_ssh_config=self.get_config().get("ssh"),
            )
        from .executor import ssh_run

        code, _, _ = ssh_run(
            ssh,
            "true",
            identity_file=host.get("identityFile"),
            global_ssh_config=self.get_config().get("ssh"),
            timeout=5,
        )
        return code == 0

    def _ensure_plugins_imported(self, cfg: dict[str, Any]) -> None:
        """Import any `[plugins] modules` once so their register_adapter() runs."""
        if getattr(self, "_plugins_imported", False):
            return
        modules = cfg.get("plugins", {}).get("modules") or []
        if modules:
            from .adapters.base import import_plugin_modules

            import_plugin_modules(list(modules))
        self._plugins_imported = True

    def list_providers(self) -> list[dict[str, str]]:
        """Resolvable LLM providers as {value, label} — built-ins plus any
        discovered plugins (entry points + `[plugins] modules`). Drives the
        Settings provider dropdown so installed plugin providers show up."""
        cfg = self.get_config()
        self._ensure_plugins_imported(cfg)
        from .adapters.base import available_providers

        labels = {
            "anthropic": "Anthropic",
            "openai": "OpenAI",
            "bedrock": "AWS Bedrock",
            "langserve": "LangServe gateway",
        }
        return [{"value": p, "label": labels.get(p, p)} for p in available_providers()]

    def _get_adapter(self, system_override: str | None = None) -> Any:
        # A system_override (used by trigger turns) builds a one-off adapter with
        # that system prompt — never served from / written to the shared cache.
        if system_override is None and self._adapter is not None:
            return self._adapter
        cfg = self.get_config()
        provider = cfg.get("llm", {}).get("provider")
        if not provider:
            return None
        self._ensure_plugins_imported(cfg)
        kwargs: dict[str, Any] = {}
        llm_cfg = cfg.get("llm", {})
        if llm_cfg.get("api_key"):
            kwargs["api_key"] = llm_cfg["api_key"]
        if llm_cfg.get("model"):
            kwargs["model"] = llm_cfg["model"]
        # Optional standing instructions for the assistant (site policy, host
        # context, tone). Only override when set so adapters keep their default.
        if str(llm_cfg.get("system", "")).strip():
            kwargs["system"] = llm_cfg["system"]
        if system_override is not None:
            kwargs["system"] = system_override  # trigger rules win for this turn
        if llm_cfg.get("aws_region"):
            kwargs["aws_region"] = llm_cfg["aws_region"]
        if llm_cfg.get("base_url"):
            kwargs["base_url"] = llm_cfg["base_url"]
        if llm_cfg.get("api_key_command"):
            kwargs["api_key_command"] = llm_cfg["api_key_command"]
        if llm_cfg.get("api_key_ttl_ms") is not None:
            kwargs["api_key_ttl_ms"] = int(llm_cfg["api_key_ttl_ms"])
        # LangServe-only knobs — describe the gateway's input/output shape. Only
        # forwarded when set (and only meaningful to the langserve adapter), so
        # other providers never see them.
        for key in (
            "auth_header",
            "auth_scheme",
            "input_messages_key",
            "input_tools_key",
        ):
            if llm_cfg.get(key):
                kwargs[key] = llm_cfg[key]
        if llm_cfg.get("tools_in_config") is not None:
            kwargs["tools_in_config"] = bool(llm_cfg["tools_in_config"])
        # The block above is an allowlist of keys the built-in adapters
        # understand. A plugin adapter (entry point / dotted path / registered)
        # may require its own [llm] config keys — rather than grow the allowlist
        # we inspect the adapter's constructor and forward any further config
        # key that matches a declared parameter. Keys already set above win;
        # `provider` (selects the adapter) and `system` (handled above and via
        # system_override) are never forwarded raw. Built-in `**kwargs`
        # factories declare no params, so this is a no-op for them.
        from .adapters.base import adapter_config_keys

        declared = adapter_config_keys(provider)
        for key, value in llm_cfg.items():
            if key in kwargs or key in ("provider", "system"):
                continue
            if key in declared:
                kwargs[key] = value
        try:
            from .adapters.base import load_adapter

            adapter = load_adapter(provider, **kwargs)
        except ImportError as exc:
            builtins = {"anthropic", "openai", "bedrock", "langserve"}
            if provider in builtins:
                raise ImportError(
                    f"Model provider '{provider}' requires its optional dependency. "
                    f"Install it with: pip install runspec-console[{provider}]"
                ) from exc
            # A plugin provider (entry point / dotted path) resolved, but a
            # dependency is missing. Surface the real cause and point at the
            # plugin package — there is no `runspec-console[{provider}]` extra.
            raise ImportError(
                f"Model provider '{provider}' could not be loaded: {exc}. "
                f"Install the package that provides '{provider}' (and its "
                f"dependencies) into the same environment as runspec-console."
            ) from exc
        except ValueError as exc:
            raise ValueError(f"Model configuration error: {exc}") from exc
        # When a key-vending command is configured, don't cache the adapter — the
        # adapter's own TTL controls refresh. Static-key configs cache as before.
        # A system_override adapter is one-off and never cached.
        if system_override is None and not llm_cfg.get("api_key_command"):
            self._adapter = adapter
        return adapter

    # ── event triggers ────────────────────────────────────────────────────────

    def _now_context(self) -> str:
        """A per-turn system-prompt block giving the agent the operator's current
        local date, so it resolves relative dates ("last Friday", "yesterday")
        against the real machine clock instead of guessing from its training
        cutoff. Folded into every agent turn (chat, trigger, schedule).

        Date-only (weekday + ISO date, no clock time) on purpose: it changes once
        a day, so the composed system prompt stays byte-identical across a day's
        turns and the model-side prompt cache keeps hitting (a per-minute
        timestamp would miss the cache every turn). Local time — ``datetime.now()``
        — because "last Friday" is the operator's local calendar."""
        today = datetime.now()
        return (
            "## Current date\n"
            f"The operator's machine date is **{today.strftime('%A, %Y-%m-%d')}** "
            '(local time). Resolve relative dates like "last Friday", "yesterday", '
            'or "next week" against this — do not assume any other date from training.'
        )

    def _base_system(self) -> str:
        """Base system prompt for trigger agent turns: the configured assistant
        instructions if set, else a sensible default. compose_system() layers
        the per-trigger rules on top of this."""
        configured = str(self.get_config().get("llm", {}).get("system", "")).strip()
        if configured:
            return configured
        return (
            "You are an assistant embedded in runspec-console. You can run "
            "runspec runnables on local and remote hosts via tools. Use tools "
            "when they help, and confirm before anything destructive."
        )

    def _console_context(self) -> str:
        """A per-turn system-prompt block making the chat agent aware of the
        working-hours rota and the configured triggers/schedules, so it can reason
        about them and react when asked to act outside working hours. Folded into
        the per-turn ``system_override`` (like the pinned-memory header), and
        returns ``""`` when nothing is configured so an unconfigured console keeps
        the fully prompt-cached fast path."""

        from .config import read_mailbox, read_rota, read_triggers
        from .sources import outlook as ol
        from .triggers import _DAYS, within_rota

        rota = read_rota()
        triggers = read_triggers()
        schedules = self._read_schedules()
        mailbox = read_mailbox() if ol.outlook_available() else None
        host_groups = self._groups_map()
        rota_on = bool(rota.get("enabled"))
        if (
            not rota_on
            and not triggers
            and not schedules
            and not mailbox
            and not host_groups
        ):
            return ""  # nothing to say — keep the cached system prefix

        lines: list[str] = ["## Console context (live automation state)"]

        if self._break_until and datetime.now() < self._break_until:
            lines.append(
                f"On break until {self._break_until.strftime('%H:%M')} — triggers and "
                "schedules are paused; they auto-resume then (held longer if the "
                "operator's screen is still locked)."
            )

        if rota.get("lock_suspend", True):
            lines.append(
                "Lock auto-suspend: ON — reactive triggers only fire while the "
                "operator's screen is unlocked (present at the workstation). "
                "Pre-scheduled jobs are exempt. This is absolute (no per-trigger "
                "override)."
            )

        if not rota_on:
            lines.append(
                "Working-hours rota: OFF — triggers and schedules run at any time."
            )
        else:
            now = datetime.now()
            inside = within_rota(rota, now)
            src = (
                "Outlook calendar"
                if rota.get("source") == "outlook"
                else "console grid"
            )
            day = rota.get(_DAYS[now.weekday()])
            if isinstance(day, dict) and day.get("on"):
                today = f"today ({now.strftime('%a')}) {day.get('start', '?')}–{day.get('end', '?')}"
            else:
                today = f"today ({now.strftime('%a')}) is a non-working day"
            note = (
                " (the live gate also consults your Outlook calendar, so treat this as approximate)"
                if rota.get("source") == "outlook"
                else ""
            )
            lines.append(
                f"Working-hours rota: ON ({src}); {today}. Right now it is "
                f"**{'INSIDE' if inside else 'OUTSIDE'}** working hours{note}. "
                "Automated triggers and schedules do not fire outside these hours. "
                "Interactive requests like this one are not auto-blocked — but when it "
                "is outside working hours and the user asks you to run, schedule, or "
                "trigger something with side effects, point out that it's outside their "
                "working hours and confirm they want to proceed before acting."
            )

        if triggers:
            lines.append("")
            lines.append(f"Configured triggers ({len(triggers)}):")
            for t in triggers[:20]:
                state = "" if t.get("enabled", True) else " [disabled]"
                lines.append(
                    f"- {t.get('name')}: {t.get('source')}:{t.get('event')} · "
                    f"{t.get('mode', 'agent')}{state}"
                )
            if len(triggers) > 20:
                lines.append(f"- … and {len(triggers) - 20} more")

        if schedules:
            lines.append("")
            lines.append(f"Configured schedules ({len(schedules)}):")
            for s in schedules[:20]:
                when = s.get("when") or {}
                when_txt = (
                    f"every {when['every']}"
                    if when.get("every")
                    else str(when.get("cron", "?"))
                )
                action = str(s.get("action") or "runnable")
                if action == "event":
                    target = f"emit {s.get('event')}"
                elif action == "prompt":
                    target = f"agent prompt: {str(s.get('prompt') or '')[:50]}"
                elif action == "runbook":
                    target = f"agent runbook {s.get('runbook')}"
                else:
                    where = (
                        f"group:{s.get('group')}" if s.get("group") else s.get("host")
                    )
                    target = f"run /{s.get('runnable')} on {where}"
                state = "" if s.get("enabled", True) else " [disabled]"
                lines.append(f"- {s.get('id')}: {target} · {when_txt}{state}")
            if len(schedules) > 20:
                lines.append(f"- … and {len(schedules) - 20} more")
            lines.append(
                "You can add schedules with the create_schedule tool — including "
                "action='prompt' (run an agent turn on a fixed instruction, to "
                "check or do something later) and action='runbook' (run a saved "
                "runbook on a cadence)."
            )

        if host_groups:
            with self._lock:
                connected = dict(self._connected_cache)
            lines.append("")
            lines.append(
                f"Host groups ({len(host_groups)}) — use a group name as the `host` "
                "value of run_runnable to fan out to EVERY host in it at once (one "
                "grouped confirm, most-restrictive autonomy wins):"
            )
            for gname, members in list(host_groups.items())[:8]:
                up = sum(1 for m in members if connected.get(m, False))
                shown = members[:8]
                more = f", +{len(members) - 8} more" if len(members) > 8 else ""
                lines.append(
                    f"- {gname}: {up}/{len(members)} connected — "
                    f"{', '.join(shown)}{more}"
                )
            if len(host_groups) > 8:
                lines.append(f"- … and {len(host_groups) - 8} more")

        if mailbox:
            lines.append("")
            where = mailbox.get("address") or "your default account"
            if mailbox.get("folder"):
                where += f" / {mailbox['folder']}"
            lines.append(
                f"Outlook (Classic, desktop): monitoring {where} for new mail"
                + (f" — label '{mailbox['label']}'" if mailbox.get("label") else "")
                + ". You can read, search, draft, send, manage your calendar, and "
                "triage (mark read / categorize / move) via the outlook_* tools "
                "(outlook_inbox / outlook_search / outlook_get_message / "
                "outlook_calendar / outlook_reminders / outlook_resolve_recipient are "
                "read-only; outlook_draft / outlook_send / outlook_save_attachment / "
                "outlook_create_event / outlook_respond_invite / outlook_mark_read / "
                "outlook_categorize / outlook_move write — sends, invites and "
                "responses are confirm-gated)."
            )

        from . import repos as repos_mod

        if repos_mod.available():
            registered = repos_mod.read_repos()
            if registered:
                lines.append("")
                names = ", ".join(r["name"] for r in registered)
                lines.append(
                    f"Git repos (local checkouts): {names}. Use the repo_* tools to "
                    "ground answers in real code — repo_status / repo_list / repo_read "
                    "/ repo_grep / repo_diff are read-only; repo_sync (clone/fetch) and "
                    "repo_write (local file edit) are confirm-gated. Edits are LOCAL "
                    "ONLY — no commit/push; review them with repo_diff."
                )

        # One-liner about the per-session workspace so the agent reaches for the
        # workspace_* tools when a spilled envelope shows up. Deliberately
        # id-free (the envelope tells you the file path each time) so the
        # cached system prefix stays stable across turns of one conversation.
        lines.append("")
        lines.append(
            "Session workspace: a runnable that emits more than a small page "
            "of output is **spilled** to disk instead of inlined — its tool "
            "result is a short envelope with a head preview and a "
            "``tool-results/…`` path. Use workspace_ls to see what's there, "
            "workspace_read / workspace_head / workspace_tail to page through "
            "a file, workspace_grep to search, and workspace_write to save "
            "a distilled note. All autonomous; scoped to this session only."
        )

        return "\n".join(lines)
