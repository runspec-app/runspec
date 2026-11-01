"""bridge_internals.py — InternalsMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod
from .config import (
    filters_path,
)
from .discovery import discover_local, discover_remote
from .hosts import group_members, groups_map, host_in_profile, profile_members

from .bridge_const import (
    DEFAULT_SYSTEM_PROMPT,
    PUTTY_LOOK_SESSION,
    PUTTY_PROXY_SESSION,
    WORKSPACE_GUIDANCE,
    logger,
)
from .bridge_utils import (
    _ensure_putty_proxy_session,
    _parse_ssh,
)

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

# Minimum spacing (s) between mid-cycle discovery progress dispatches — see
# _refresh_cycle_locked. The final end-of-cycle dispatch is never throttled.
_DISCOVERY_DISPATCH_MIN_INTERVAL = 0.5


def _runnables_fingerprint(entries: list[dict[str, Any]]) -> str:
    """Order-sensitive content key for one host's discovered runnables — equal
    when a re-discovery found exactly what the cache already held."""
    return json.dumps(entries, sort_keys=True, default=str)


# Same for the probe phase's per-host hosts_updated (sidebar dots).
_PROBE_DISPATCH_MIN_INTERVAL = 0.5


class InternalsMixin:
    def _putty_command(self, host: str) -> list[str]:
        """The ``putty.exe`` argv that opens an SSH window to ``host``.

        Raises ``ValueError`` (with an operator-readable message) when the host
        is unknown/local, PuTTY isn't installed, or the configured key is
        missing — shared by ``launch_terminal`` and ``launch_terminals``.
        """
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
        cfg = self.get_config()
        cfg_ssh = cfg.get("ssh", {})
        user, hostname, port = _parse_ssh(ssh)
        if not user:
            user = cfg_ssh.get("user", "")

        # Prefer PPK v2 (works with all PuTTY versions) over raw OpenSSH key.
        key_file: Path | None = None
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

        cmd = [str(putty)]
        # Route through the HTTP proxy by loading a saved session that carries
        # the proxy config (PuTTY has no proxy CLI flag). Host/user/key below
        # still override the session.
        # The [terminal] look (font, colours, cursor) rides the same saved
        # sessions; a host whose group has its own theme gets its own session.
        from . import terminal_look

        look_cfg = terminal_look.read_terminal(cfg)
        group = str(entry.get("group") or "")
        look = terminal_look.session_values(look_cfg, group)
        suffix = terminal_look.session_suffix(look_cfg, group) if look else ""
        proxy = cfg_ssh.get("proxy") or ""
        proxy_session = (
            _ensure_putty_proxy_session(
                proxy, PUTTY_PROXY_SESSION + suffix, look=look or None
            )
            if proxy
            else None
        )
        jump = utils_mod.host_jump(getattr(self, "_hosts", None), entry)
        if jump:
            # Through a jump host: a saved session whose proxy is a local
            # `plink -nc` command — works on PuTTY versions without the SSH
            # proxy type. The HTTP proxy (if any) moves onto the plink hop.
            plink = utils_mod._find_putty_exe("plink.exe")
            if plink is None:
                raise ValueError(
                    f"plink.exe not found — it is needed to open a terminal to "
                    f"{host} through jump host {jump}. It ships with PuTTY "
                    r"(expected at C:\Program Files\PuTTY\plink.exe)."
                )
            proxy_cmd = utils_mod._putty_jump_proxy_command(
                str(plink),
                jump,
                user=str(cfg_ssh.get("user") or ""),
                key_file=str(key_file) if key_file else "",
                proxy_session=proxy_session or "",
            )
            session = (
                utils_mod._ensure_putty_jump_session(
                    jump, proxy_cmd, look=look, suffix=suffix
                )
                if look
                else utils_mod._ensure_putty_jump_session(jump, proxy_cmd)
            )
            if not session:
                raise ValueError(
                    "Opening a PuTTY terminal through a jump host needs Windows "
                    "(the jump is stored as a PuTTY saved session)."
                )
            cmd.extend(["-load", session])
        elif proxy_session:
            cmd.extend(["-load", proxy_session])
        elif look:
            look_session = utils_mod._ensure_putty_look_session(
                look, PUTTY_LOOK_SESSION + suffix
            )
            if look_session:
                cmd.extend(["-load", look_session])
        cmd.append("-ssh")
        if user:
            cmd.extend(["-l", user])
        cmd.append(hostname)
        if port:
            cmd.extend(["-P", str(port)])
        if key_file is not None:
            cmd.extend(["-i", str(key_file)])
        return cmd

    def launch_terminal(self, host: str) -> None:
        """Launch PuTTY in a separate window connected to the host.

        Fire-and-forget: no stdout/stderr capture, no session tracking. PuTTY
        runs as its own top-level process and the user closes it from there.
        """
        import subprocess as _sp

        _sp.Popen(self._putty_command(host))

    def launch_terminals(
        self, hosts: list[str], tile: bool = True, layout: str = "grid"
    ) -> dict[str, Any]:
        """Open a PuTTY window on each of ``hosts`` at once, optionally tiled.

        Each host is launched independently — one that fails (unknown, no key,
        PuTTY missing) is reported in ``errors`` and the rest still open. With
        ``tile`` the windows are then arranged on the console's monitor by a
        background thread (Windows only, best-effort), so this returns as soon
        as the processes are spawned. ``layout`` is one of ``grid`` /
        ``columns`` / ``rows`` / ``cascade`` (``wintile.LAYOUTS``; anything
        else ⇒ ``grid``).

        Returns ``{"opened": [host…], "errors": [{"host", "error"}…],
        "tiling": bool}``.
        """
        import subprocess as _sp

        opened: list[str] = []
        errors: list[dict[str, str]] = []
        pids: list[int] = []
        seen: set[str] = set()
        for host in hosts or []:
            name = str(host)
            if not name or name in seen:
                continue
            seen.add(name)
            try:
                proc = _sp.Popen(self._putty_command(name))
            except (ValueError, OSError) as exc:
                errors.append({"host": name, "error": str(exc)})
                continue
            opened.append(name)
            pids.append(proc.pid)
        tiling = bool(tile and len(pids) > 1 and sys.platform == "win32")
        if tiling:
            from . import wintile

            threading.Thread(
                target=wintile.tile_process_windows,
                args=(pids,),
                kwargs={"layout": layout if layout in wintile.LAYOUTS else "grid"},
                name="tile-terminals",
                daemon=True,
            ).start()
        return {"opened": opened, "errors": errors, "tiling": tiling}

    def terminal_themes(self) -> list[dict[str, Any]]:
        """The PuTTY colour themes for Settings → SSH → Terminal look (id,
        label and colours as hex, for the preview)."""
        from . import terminal_look

        return terminal_look.themes_for_ui()

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

    def _unknown_credential_error(self, credential_id: str) -> str | None:
        """An agent-readable error when ``credential_id`` names no saved
        credential, or ``None`` when it resolves.

        Reloads the store first so a credential added since start-up isn't
        reported missing. A store that can't be read returns ``None`` — the run
        proceeds and the downstream warn-and-skip still applies; refusing on an
        unreadable store would turn a credentials problem into a run outage."""
        cid = str(credential_id or "").strip()
        if not cid:
            return None
        try:
            self._reload_credentials()
        except Exception:  # noqa: BLE001 — an unreadable store must not block a run
            logger.debug("credential store unavailable for validation", exc_info=True)
            return None
        if self._credential_entry(cid) is not None:
            return None
        known = sorted(str(c.get("id", "")) for c in self._credentials if c.get("id"))
        return (
            f"Error: unknown credential_id '{cid}'. "
            + (
                f"Saved credentials: {', '.join(known)}."
                if known
                else "No credentials are saved on this console."
            )
            + " In-scope credentials apply automatically — omit credential_id "
            "unless you need to force a specific one."
        )

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

    @staticmethod
    def _has_scope(entry: dict[str, Any]) -> bool:
        """True when a credential names at least one scope pattern (host or
        runnable). ``["*"]`` counts — it's how an operator deliberately says
        "everywhere"."""
        return any(
            str(p).strip()
            for key in ("scope_hosts", "scope_runnables")
            for p in (entry.get(key) or [])
        )

    def _declared_interface(
        self, host: str, runnable: str
    ) -> tuple[set[str], set[str]]:
        """``(env var names, arg names)`` the runnable declares on ``host``, across
        every subcommand — what an unscoped credential is matched against."""
        envs: set[str] = set()
        names: set[str] = set()

        def walk(node: dict[str, Any]) -> None:
            for a in node.get("args") or []:
                if not isinstance(a, dict):
                    continue
                if a.get("name"):
                    names.add(str(a["name"]))
                env = a.get("env")
                for e in [env] if isinstance(env, str) else list(env or []):
                    if str(e).strip():
                        envs.add(str(e).strip().upper())
            for sub in (node.get("commands") or {}).values():
                if isinstance(sub, dict):
                    walk(sub)

        for r in list(getattr(self, "_runnables_cache", []) or []):
            if r.get("name") == runnable and (r.get("host") or "local") == host:
                walk(r)
        return envs, names

    @staticmethod
    def _declared_use(entry: dict[str, Any], envs: set[str], names: set[str]) -> bool:
        """True when the runnable's own interface asks for this credential: an
        arg ``env`` equal to one of the credential's name-derived env vars, or an
        arg the credential binds via ``username_arg``/``secret_arg``."""
        from . import credentials as cred

        wanted = {
            n.upper()
            for n in cred.env_var_names(
                str(entry.get("label", "")), str(entry.get("kind", ""))
            )
        }
        if wanted & envs:
            return True
        return any(
            str(entry.get(k) or "").strip() in names
            for k in ("username_arg", "secret_arg")
            if str(entry.get(k) or "").strip()
        )

    def _scoped_credentials(self, host: str, runnable: str) -> list[dict[str, Any]]:
        """Credentials that auto-apply to ``(host, runnable)``.

        * **Scoped** (either scope list non-empty): both lists must match — an
          empty list on one side matches anything, so ``scope_hosts=["web-*"]``
          alone means "every runnable on web hosts". ``["*"]`` = everywhere, on
          purpose.
        * **Unscoped** (both lists empty): applies only where the runnable
          *declares* it — an arg ``env`` equal to one of the credential's env var
          names, or an arg its ``username_arg``/``secret_arg`` binds. It used to
          mean "every runnable on every host", which sent a credential made for
          one tool (or for the console itself) to every host in the fleet.

        Explicit selection (Forms picks, an agent ``credential_id``) is handled by
        the callers and is unaffected. Reads the cache as-is."""
        out: list[dict[str, Any]] = []
        interface: tuple[set[str], set[str]] | None = None
        for c in self._credentials:
            if self._has_scope(c):
                if self._scope_matches(
                    c.get("scope_hosts"), host
                ) and self._scope_matches(c.get("scope_runnables"), runnable):
                    out.append(c)
                continue
            if interface is None:
                interface = self._declared_interface(host, runnable)
            if self._declared_use(c, *interface):
                out.append(c)
        return out

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
        """Synthetic entry for the local machine — the venv runspec-console itself
        runs in, plus any additional venvs matched by the ``[local] venv_globs``
        convention (``config.read_local`` → ``hosts.resolve_local_venvs``). The
        console's own venv is always first (the default). Everything downstream is
        list-driven (per-venv discovery, group-matched execution, per-venv sidebar
        counts), so extra local venvs light up with no other change."""
        scripts = Path(sys.executable).parent
        # Windows venvs use Scripts\runspec.exe; Linux/Mac use bin/runspec.
        # Fallback: the expected path even if runspec isn't installed yet.
        own = str(scripts / "runspec.exe")
        for name in ("runspec.exe", "runspec"):
            candidate = scripts / name
            if candidate.exists():
                own = str(candidate)
                break
        globs = config_mod.read_local().get("venv_globs") or []
        paths = hosts_mod.resolve_local_venvs(globs, own_path=own)
        return {"name": "local", "runspec_paths": paths, "ssh": None}

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

    def _configured_host_names(self) -> set[str]:
        """Names of **every** configured host (local + all remotes, ignoring the
        active profile) — the set automation may target on demand. Reloads from
        disk via ``_all_host_entries``. Used by the agent-turn resolvers to accept
        a runnable on a configured-but-currently-cold host (execution then
        lazy-connects), matching the "automation reaches any host" invariant."""
        return {str(h.get("name")) for h in self._all_host_entries()}

    def _fallback_for_host(
        self, host: str, name: str, hosts: list[str], template: dict[str, Any]
    ) -> dict[str, Any] | str:
        """Resolve a runnable on a host whose cache doesn't list it.

        A configured host that has **not been discovered** (out of the active
        profile, not yet probed, discovery failed) is a valid target: execution
        lazy-connects, so borrow a warm host's definition with the host swapped.
        A host that **has** been discovered and simply doesn't carry the runnable
        is not — borrowing there runs a binary that isn't installed (exit 127,
        "No such file or directory"), so say so and name where it is. Returns the
        synthesized cache entry, or an error string the agent can act on."""
        options = ", ".join(hosts)
        if host not in self._configured_host_names():
            return f"Error: host '{host}' does not have '{name}'. Options: {options}."
        with self._lock:
            discovered = any(
                r.get("host", "local") == host for r in self._runnables_cache
            )
        if discovered:
            return (
                f"Error: '{name}' is not installed on '{host}' (its runnables "
                f"were discovered and '{name}' is not among them — it may need "
                f"installing or upgrading there). Available on: {options}."
            )
        # The venv owner / login account were found on the template's host, not
        # this one: unknown here ⇒ no escalation (core refuses if it needed one).
        return {**template, "host": host, "venvOwner": None, "loginUser": ""}

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

    def _profile_members(self, profile: str) -> list[str]:
        """Host names tagged with host profile ``profile`` (see
        :func:`hosts.profile_members`). A blank name yields no hosts — callers
        targeting a profile always name one."""
        if not str(profile or "").strip():
            return []
        return profile_members(self._all_host_entries(), profile)

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
                if not self._refresh_tick():
                    return  # interpreter is shutting down — stop the watcher
                interval = refresh_settings(self.get_config().get("refresh"))[
                    "interval"
                ]
                time.sleep(interval)

        threading.Thread(target=_loop, daemon=True).start()

    def _refresh_tick(self) -> bool:
        """Run one refresh cycle. Returns ``False`` when the process is exiting so
        the daemon watcher loop stops quietly, ``True`` to keep looping.

        On app close this daemon thread can wake from its sleep and reach
        ``_refresh_cycle``'s ``ThreadPoolExecutor.submit`` *after* the interpreter
        has begun shutting down, which raises ``RuntimeError: cannot schedule new
        futures after interpreter shutdown``. That's teardown, not a fault — swallow
        it and stop the loop instead of letting the thread die with a traceback.
        Any other error keeps the old behaviour (propagates)."""
        try:
            self._refresh_cycle()
        except RuntimeError as exc:
            if "cannot schedule new futures" in str(exc):
                return False
            raise
        return True

    def _fanout_workers(self) -> int:
        """Bounded worker count for a fleet-wide fan-out outside the refresh cycle
        (denylist status / ``runspec deny`` across hosts): the same ``[refresh]
        workers`` bound the cycle uses, so one knob sizes every fleet sweep and
        the handshake storm stays capped by ``[ssh] max_concurrent`` underneath."""
        from .ssh_pool import pool_settings, refresh_settings

        cfg = self.get_config()
        max_concurrent = pool_settings(cfg.get("ssh"))["max_concurrent"]
        settings = refresh_settings(cfg.get("refresh"), max_concurrent=max_concurrent)
        return max(1, int(settings.get("workers") or 16))

    def _probe_one(
        self,
        h: dict[str, Any],
        *,
        jitter: float = 0,
        ssh_cfg: dict[str, Any] | None = None,
    ) -> None:
        """One host's connectivity probe (a refresh-cycle worker task): optional
        start jitter to spread connects, then record the result in the cache.
        ``ssh_cfg`` is the cycle's one-shot read of ``[ssh]`` (avoids a config.toml
        read per host)."""
        if jitter:
            time.sleep(random.uniform(0, jitter))  # spread the connects
        result = self._check_connected(h, ssh_cfg=ssh_cfg)
        with self._lock:
            changed = self._connected_cache.get(h["name"]) is not result
            self._connected_cache[h["name"]] = result
        # Unattended connection flips are exactly what audio awareness is for —
        # the interactive flows (test host, copy key) already have visual
        # feedback, so only the refresh-loop probe cues.
        self._connection_cue("host", h["name"], result)
        # Let the sidebar turn this host green/grey *now* rather than when the
        # whole fleet's probe finishes (tens of seconds on a large fleet, during
        # which the cue had already played) — coalesced so a 70-host sweep is a
        # handful of refetches, not 70. The phase-end dispatch is unconditional.
        if changed:
            self._dispatch_coalesced(
                "runspec:hosts_updated", {}, min_interval=_PROBE_DISPATCH_MIN_INTERVAL
            )

    def _discover_one(
        self, h: dict[str, Any], *, ssh_cfg: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """One host's runnable discovery (a refresh-cycle worker task). Returns the
        discovered runnables (empty if the host was marked disconnected by the
        probe phase), so the caller can flatten results without a shared lock.
        ``ssh_cfg`` is the cycle's one-shot read of ``[ssh]``; ``None`` reads it."""
        if ssh_cfg is None:
            ssh_cfg = self.get_config().get("ssh")
        ssh = h.get("ssh")
        name = h["name"]
        with self._lock:
            connected = self._connected_cache.get(name, ssh is None)
        if not connected:
            return []
        jump = utils_mod.host_jump(getattr(self, "_hosts", None), h)
        if jump and self._jump_mode(h, ssh_cfg) == "ssh":
            # System-ssh transport: discover over the jump host's OpenSSH so a
            # legacy target's ssh-rsa/ssh-dss handshake works (paramiko can't).
            # A "pool" jump host falls through to the pool, which tunnels it.
            def runner(
                target: str,
                command: str,
                *,
                identity_file: str | None = None,
                global_ssh_config: dict[str, Any] | None = None,
                timeout: int = 15,
                _jump: str = jump,
            ) -> tuple[int, str, str]:
                return executor_mod.ssh_jump_run(
                    target,
                    command,
                    jump=_jump,
                    identity_file=identity_file,
                    global_ssh_config=global_ssh_config,
                    timeout=timeout,
                )
        else:
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
                    global_ssh_config=ssh_cfg,
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

    # ── discovery cadence ────────────────────────────────────────────────────
    #
    # Connectivity is probed every cycle (cheap: a warm pooled transport), but a
    # host's runnables are re-discovered — one ``runspec local`` SSH exec per venv
    # — only when something could have changed: first sight, a new transport
    # (reconnect), an edited path/identity, ``[refresh] discovery_interval``
    # elapsed, or an explicit force (host save, profile switch, Refresh button).
    # On a 70-host fleet this removes ~70 SSH execs and ~140 UI events from every
    # steady-state 30 s cycle.

    @staticmethod
    def _discovery_key(h: dict[str, Any]) -> tuple:
        """What a host's discovery depends on — a change forces re-discovery."""
        return (
            tuple(utils_mod._paths(h)),
            str(h.get("ssh") or ""),
            str(h.get("identityFile") or ""),
            str(h.get("jump") or "").strip(),
        )

    def _invalidate_discovery(self, names: Iterable[str] | None = None) -> None:
        """Forget the last-discovered stamp for ``names`` (all when ``None``) so
        the next cycle re-discovers them — the targeted alternative to a forced
        cycle (e.g. after a key was copied to one host)."""
        with self._lock:
            if names is None:
                self._discovery_stamp.clear()
            else:
                for n in names:
                    self._discovery_stamp.pop(n, None)

    def _transport_generation(
        self, h: dict[str, Any], ssh_cfg: dict[str, Any] | None
    ) -> int:
        """The pool's transport generation for ``h`` (0 for local / system-ssh
        jump / no pool)."""
        pool = self._ssh_pool
        ssh = h.get("ssh")
        if pool is None or not ssh or self._jump_mode(h, ssh_cfg) == "ssh":
            return 0
        try:
            return pool.generation(
                ssh, identity_file=h.get("identityFile"), global_ssh_config=ssh_cfg
            )
        except Exception:  # noqa: BLE001 — a diagnostic read never blocks the cycle
            return 0

    def _should_discover(
        self,
        h: dict[str, Any],
        *,
        now: float,
        force: bool,
        discovery_interval: int,
        ssh_cfg: dict[str, Any] | None,
    ) -> bool:
        """Decide whether this cycle re-discovers ``h`` (see the cadence note).
        A disconnected host is never discovered (its stamp is dropped so the
        disconnected→connected transition re-discovers)."""
        name = h["name"]
        with self._lock:
            connected = self._connected_cache.get(name, h.get("ssh") is None)
            stamp = self._discovery_stamp.get(name)
            if not connected:
                self._discovery_stamp.pop(name, None)
                return False
        if force or stamp is None or discovery_interval <= 0:
            return True  # (0 ⇒ discover every cycle, the pre-0.265 behaviour)
        ts, gen, key = stamp
        if key != self._discovery_key(h):
            return True
        if self._transport_generation(h, ssh_cfg) != gen:
            return True
        return (now - ts) >= discovery_interval

    def _dispatch_coalesced(
        self, event: str, detail: dict[str, Any], *, min_interval: float
    ) -> bool:
        """Dispatch ``event`` unless one went out less than ``min_interval`` s
        ago — for per-host progress signals during a fleet sweep, where each
        dispatch is a synchronous evaluate_js and a frontend refetch. Returns
        whether it was sent; a phase's final dispatch should be unconditional."""
        now = time.monotonic()
        with self._lock:
            last = self._coalesced_at.get(event, 0.0)
            if now - last < min_interval:
                return False
            self._coalesced_at[event] = now
        self._dispatch(event, detail)
        return True

    def _kick_refresh(self, *, force: bool = False) -> None:
        """Run one refresh cycle on a daemon thread (returns immediately). Bumps
        the refresh epoch so a cycle already in progress yields at its next task
        boundary — a profile switch or Refresh click takes effect within one
        host's probe instead of after the whole fleet's."""
        with self._lock:
            self._refresh_epoch += 1
        if force:
            # A forced refresh is an operator action (host save, profile switch,
            # Refresh): try every host now instead of after its connect-backoff
            # window, which silently skipped hosts that had failed earlier.
            pool = getattr(self, "_ssh_pool", None)
            if pool is not None:
                pool.clear_backoff()
            # …and gives jump hosts that fell back to system ssh another try
            # at the pooled tunnel (the jump or target may have been fixed).
            self._clear_jump_ssh_pins()
        threading.Thread(
            target=lambda: self._refresh_cycle(force=force), daemon=True
        ).start()

    def _refresh_superseded(self, epoch: int) -> bool:
        """True once a newer kicked refresh has been requested since ``epoch``."""
        with self._lock:
            return self._refresh_epoch != epoch

    def refresh_hosts(self) -> None:
        """Probe every in-scope host and re-discover its runnables now (the
        tool-count click at the bottom of the hosts panel) — a forced cycle,
        ignoring the discovery stamps."""
        self._kick_refresh(force=True)

    def _refresh_cycle(self, *, force: bool = False) -> None:
        """Probe connectivity, then discover runnables — reusing pooled connections.

        ``force`` re-discovers every connected host regardless of its stamp
        (host save / profile switch / manual refresh); otherwise only hosts
        ``_should_discover`` picks are re-run and the rest keep their cached
        runnables. Cycles are serialised by ``_refresh_cycle_lock``.

        With the pool on, Phase 1 *warms* the per-host transport that Phase 2's
        discovery then reuses, so a host costs one handshake per cycle (often
        zero once warm) instead of the old ``probe + per-path`` handshake storm.
        Per-host jitter spreads the cycle's connects so they don't all fire on
        the same instant; the pool's global semaphore bounds how many connect at
        once; ``reap()`` closes transports gone idle.
        """
        with self._refresh_cycle_lock:
            self._refresh_cycle_locked(force=force)

    def _refresh_cycle_locked(self, *, force: bool = False) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from .ssh_pool import pool_settings, refresh_settings

        # Only the active profile's hosts are probed and kept warm — an unloaded
        # profile's hosts hold no standing connection (the point of profiles).
        # Automation still reaches out-of-scope hosts on demand via _host_entry /
        # _all_host_entries; the pool idle-reaps those transient transports.
        all_hosts = self._active_host_entries()
        # config.toml is read ONCE per cycle and threaded into every worker —
        # it used to be re-read (disk + TOML parse under the config lock) per
        # host, per venv, and per probe.
        cfg = self.get_config()
        # ``{}`` (not None) when the section is absent, so the workers' "None ⇒
        # read config" fallback never triggers inside the cycle.
        ssh_cfg: dict[str, Any] = dict(cfg.get("ssh") or {})
        max_concurrent = pool_settings(ssh_cfg)["max_concurrent"]
        settings = refresh_settings(cfg.get("refresh"), max_concurrent=max_concurrent)
        jitter = settings["jitter"]
        # Bounded worker count for the fan-out — see refresh_settings. A patched
        # refresh_settings (tests) may omit it; fall back to a safe default.
        workers = max(1, int(settings.get("workers") or 16))
        try:
            discovery_interval = max(0, int(settings.get("discovery_interval", 300)))
        except (TypeError, ValueError):
            discovery_interval = 300
        # A kicked refresh (profile switch / host save / Refresh button) bumps the
        # epoch; this cycle yields at its next task boundary once that happens so
        # the kick doesn't wait behind a whole fleet sweep.
        with self._lock:
            epoch = self._refresh_epoch
        total = len(all_hosts)
        # Tell the UI a sweep is under way *before* the probe — on a large fleet
        # the probe alone is tens of seconds, and this used to be the first sign
        # of life after launch.
        self._dispatch(
            "runspec:discovery_state",
            {"done": 0, "total": total, "ready": self._discovery_ready},
        )

        # ── Phase 1: connectivity (warms the pooled transport) ─────────────────
        # A bounded pool instead of one thread per host: at large fleets this
        # holds a constant ~workers threads rather than ~N. Order is preserved —
        # the whole probe phase completes before discovery (which skips hosts
        # the probe marked disconnected) runs; the sidebar is updated
        # progressively by _probe_one as hosts land.
        with self._lock:
            self._connected_cache["local"] = True

        def _probe(h: dict[str, Any]) -> None:
            if self._refresh_superseded(epoch):
                return  # a newer refresh is waiting — leave this host to it
            self._probe_one(h, jitter=jitter, ssh_cfg=ssh_cfg)

        ssh_hosts = [h for h in all_hosts if h.get("ssh")]
        if ssh_hosts:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(ssh_hosts)),
                thread_name_prefix="refresh-probe",
            ) as ex:
                list(ex.map(_probe, ssh_hosts))
        self._dispatch("runspec:hosts_updated", {})
        if self._refresh_superseded(epoch):
            logger.debug("refresh: superseded after probe phase, yielding")
            return

        # ── Phase 2: runnables discovery (reuses the warm transport) ───────────
        # Only hosts whose discovery could be stale are re-run (see
        # _should_discover); the rest keep their cached entries. Each re-run
        # host's runnables are merged into the cache *as it completes* rather
        # than waiting for the whole fleet, so local (no SSH — near-instant) and
        # fast hosts are usable on the very first turn instead of being blocked
        # behind the slowest bastion. Dedup is per-host (the key includes the
        # host, so two hosts never collide); a removed host is pruned by the
        # canonical assign at the end of the cycle.
        now = time.monotonic()
        to_discover = [
            h
            for h in all_hosts
            if self._should_discover(
                h,
                now=now,
                force=force,
                discovery_interval=discovery_interval,
                ssh_cfg=ssh_cfg,
            )
        ]
        rerun_names = {h["name"] for h in to_discover}
        # A disconnected host contributes nothing (its stale entries drop), a
        # skipped-but-connected host keeps what it had.
        with self._lock:
            keep_names = {
                h["name"]
                for h in all_hosts
                if h["name"] not in rerun_names
                and self._connected_cache.get(h["name"], h.get("ssh") is None)
            }
            previous_hosts = {r.get("host", "") for r in self._runnables_cache}
            previous_by_host: dict[str, list[dict[str, Any]]] = {}
            for r in self._runnables_cache:
                previous_by_host.setdefault(r.get("host", ""), []).append(r)
            accumulated: list[dict[str, Any]] = [
                r for r in self._runnables_cache if r.get("host", "") in keep_names
            ]

        done = total - len(to_discover)
        # Progress dispatches are coalesced: each one is a synchronous
        # evaluate_js on the single UI-dispatch thread AND makes the frontend
        # re-fetch the whole catalogue, so per-host emission on a large fleet was
        # an event storm that slowed the entire app. Emit at most one per
        # _DISCOVERY_DISPATCH_MIN_INTERVAL mid-cycle; the final emit below is
        # unconditional.
        last_emit = 0.0
        # Re-discovering a host (its interval elapsed, or its transport
        # reconnected) usually finds exactly what it had. Only a real change is
        # worth a `runnables_updated` — each one makes the frontend re-fetch the
        # catalogue and pushes `tools/list_changed` to MCP clients, and a
        # no-change stream of them clogs the single UI-dispatch queue that
        # confirm cards also ride.
        content_changed = False
        if to_discover:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(to_discover)),
                thread_name_prefix="refresh-disc",
            ) as ex:

                def _discover(h: dict[str, Any]) -> list[dict[str, Any]] | None:
                    if self._refresh_superseded(epoch):
                        return None  # yield to the newer refresh
                    return self._discover_one(h, ssh_cfg=ssh_cfg)

                futures = {ex.submit(_discover, h): h for h in to_discover}
                for fut in as_completed(futures):
                    h = futures[fut]
                    host_name = h.get("name", "")
                    ok = True
                    try:
                        host_runnables = fut.result()
                    except Exception:
                        logger.debug(
                            "discovery failed for host %s", host_name, exc_info=True
                        )
                        host_runnables = []
                        ok = False
                    if host_runnables is None:
                        continue  # skipped: superseded — the next cycle re-runs it
                    cleaned = self._dedup_host_runnables(host_runnables, host_name)
                    accumulated.extend(cleaned)
                    host_changed = _runnables_fingerprint(
                        cleaned
                    ) != _runnables_fingerprint(previous_by_host.get(host_name, []))
                    content_changed = content_changed or host_changed
                    done += 1
                    # Splice this host's results into the live cache: drop its
                    # prior entries, keep every other host's, append the fresh
                    # set. Never empties the cache mid-cycle.
                    with self._lock:
                        self._runnables_cache = [
                            r
                            for r in self._runnables_cache
                            if r.get("host", "") != host_name
                        ] + cleaned
                        self._discovery_progress = (done, total)
                        if ok:
                            self._discovery_stamp[host_name] = (
                                time.monotonic(),
                                self._transport_generation(h, ssh_cfg),
                                self._discovery_key(h),
                            )
                    t = time.monotonic()
                    if t - last_emit >= _DISCOVERY_DISPATCH_MIN_INTERVAL:
                        last_emit = t
                        if content_changed:
                            self._dispatch("runspec:runnables_updated", {})
                        self._dispatch(
                            "runspec:discovery_state",
                            {
                                "done": done,
                                "total": total,
                                "ready": self._discovery_ready,
                            },
                        )

        if self._refresh_superseded(epoch):
            # Leave the cache as spliced so far; the newer refresh finishes the
            # job (and emits the final state) — no canonical assign from a
            # partial sweep.
            logger.debug("refresh: superseded during discovery, yielding")
            return

        # Canonical assign: prunes any host that left the fleet (its entries linger
        # in the spliced cache until here), and marks discovery ready after the
        # first full cycle so the readiness indicator clears.
        with self._lock:
            self._runnables_cache = accumulated
            self._discovery_ready = True
            self._discovery_progress = (total, total)
            # Stamps only for hosts still in scope — a host that leaves a profile
            # and comes back within discovery_interval must not look "fresh".
            in_scope = {h["name"] for h in all_hosts}
            for n in [n for n in self._discovery_stamp if n not in in_scope]:
                self._discovery_stamp.pop(n, None)
            current_hosts = {r.get("host", "") for r in accumulated}
        changed = content_changed or current_hosts != previous_hosts
        logger.debug(
            "discovery: %d host(s) re-discovered, %d kept (fresh), force=%s",
            len(to_discover),
            total - len(to_discover),
            force,
        )
        self._dispatch(
            "runspec:discovery_state", {"done": total, "total": total, "ready": True}
        )
        if changed:
            # A host's runnables changed or a host left — tell the UI once (a
            # cycle that re-discovered the same runnables stays silent, so the
            # frontend doesn't refetch the catalogue for nothing).
            self._dispatch("runspec:runnables_updated", {})
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
        if self._adhoc_pool is not None:
            self._adhoc_pool.reap()

    # ── jump-host routing ──────────────────────────────────────────────────────
    #
    # A host with ``jump`` is reached one of two ways:
    #
    # * "pool" — over the console's SSH pool: a ``direct-tcpip`` tunnel through
    #   the jump host's own warm pooled connection. One jump login serves every
    #   host behind it, with keepalive, backoff and channel reuse, and every
    #   pooled path (history, logs, transfers, runs) reaches the host too.
    # * "ssh"  — the system OpenSSH client per call (``executor.ssh_jump_run``),
    #   for targets paramiko can't negotiate (CentOS 6: ssh-rsa/ssh-dss only),
    #   jump chains (``a,b``), or a jump only ``~/.ssh/config`` knows how to
    #   reach.
    #
    # Default is "pool"; a host whose pooled connect fails while system ssh
    # works is switched to "ssh" for the session (until a host save, profile
    # switch or Refresh). ``[ssh] jump_pool = false`` keeps every jump host on
    # system ssh, the pre-0.369.0 behaviour.

    def _jump_mode(
        self, entry: dict[str, Any] | None, ssh_cfg: dict[str, Any] | None = None
    ) -> str:
        """``""`` (no jump), ``"pool"`` or ``"ssh"`` for this host entry."""
        jump = str((entry or {}).get("jump") or "").strip()
        if not jump:
            return ""
        if getattr(self, "_ssh_pool", None) is None or "," in jump:
            return "ssh"
        if ssh_cfg is None:
            try:
                ssh_cfg = self.get_config().get("ssh")
            except Exception:  # noqa: BLE001 — routing must never raise
                ssh_cfg = None
        if (ssh_cfg or {}).get("jump_pool", True) is False:
            return "ssh"
        name = str((entry or {}).get("name") or "")
        with self._lock:
            pinned = getattr(self, "_jump_ssh_hosts", None) or set()
            if name in pinned:
                return "ssh"
        return "pool"

    def _ssh_route(
        self,
        ssh_target: str,
        identity_file: str | None,
        ssh_cfg: dict[str, Any] | None = None,
    ) -> tuple[str, str | None] | None:
        """The pool's route resolver: ``(jump_target, jump_identity)`` for a
        fleet host in "pool" jump mode, else None. The jump is reached with the
        jump fleet host's own key, or the target's key for an alias /
        ``user@host`` jump (the same key the system-ssh hop uses)."""
        hosts = getattr(self, "_hosts", None) or []
        entry = None
        for h in hosts:
            if str(h.get("ssh") or "") == ssh_target:
                if str(h.get("identityFile") or "") == str(identity_file or ""):
                    entry = h
                    break
                if entry is None:
                    entry = h
        if entry is None or self._jump_mode(entry, ssh_cfg or {}) != "pool":
            return None
        jump = utils_mod.host_jump(hosts, entry)
        if not jump:
            return None
        jump_entry = utils_mod.host_jump_entry(hosts, entry)
        jump_idf = (
            jump_entry.get("identityFile") if jump_entry is not None else identity_file
        )
        return jump, (jump_idf or None)

    def _pin_jump_ssh(self, name: str, pinned: bool = True) -> None:
        with self._lock:
            hosts = getattr(self, "_jump_ssh_hosts", None)
            if hosts is None:
                hosts = self._jump_ssh_hosts = set()
            if pinned:
                hosts.add(name)
            else:
                hosts.discard(name)

    def _clear_jump_ssh_pins(self) -> None:
        with self._lock:
            self._jump_ssh_hosts = set()

    def _remote_session(
        self, entry: dict[str, Any], ssh_cfg: dict[str, Any] | None = None
    ) -> tuple[str, Any]:
        """``(jump, session_factory)`` for streaming a run on ``entry``:
        system-ssh jump hosts get ``(jump, None)``; everything else gets a pooled
        channel factory (``("", None)`` when pooling is off)."""
        if ssh_cfg is None:
            ssh_cfg = self.get_config().get("ssh")
        if self._jump_mode(entry, ssh_cfg) == "ssh":
            return utils_mod.host_jump(getattr(self, "_hosts", None), entry), None
        pool = self._ssh_pool
        ssh = entry.get("ssh")
        if pool is None or not ssh:
            return "", None
        idf = entry.get("identityFile")

        def session_factory() -> tuple[Any, Any]:
            return pool.open_session(ssh, identity_file=idf, global_ssh_config=ssh_cfg)

        return "", session_factory

    def _report_jump_probe(
        self, host: dict[str, Any], jump: str, code: int, detail: str
    ) -> None:
        """Make a jump-host connectivity result visible — a ``runspec:ssh`` row in
        the Dev tab plus the console log — once per change, not every cycle.

        The jump (system-ssh) transport bypasses paramiko and the pool, which is
        where every other SSH event comes from, so a failing jump host used to
        show only as a grey sidebar dot with nothing anywhere saying why.
        """
        name = str(host.get("name") or "")
        state = "" if code == 0 else (detail or f"ssh exited {code}")
        # The settings are part of the key, so editing the jump, target or key
        # reports the next attempt even when it fails the same way as before.
        settings = (
            jump,
            str(host.get("ssh") or ""),
            str(host.get("identityFile") or ""),
        )
        with self._lock:
            seen = getattr(self, "_jump_probe_state", None)
            if seen is None:
                seen = self._jump_probe_state = {}
            previous = seen.get(name)
            seen[name] = (settings, state)
        if previous == (settings, state) or (previous is None and not state):
            return
        if state:
            message = (
                f"{name}: can't connect to {host.get('ssh')} via jump host "
                f"{jump}: {state}"
            )
            # The console puts no proxy on the jump hop, so one ssh -G finds came
            # from the operator's own ssh config — the usual cause of an
            # "HTTP relay" or banner failure on that hop.
            try:
                hop_proxy = executor_mod.jump_hop_proxy(
                    jump, self.get_config().get("ssh")
                )
            except Exception:  # noqa: BLE001 — a diagnostic never blocks the probe
                hop_proxy = ""
            if hop_proxy:
                message += (
                    f" (your ssh config gives {jump} '{hop_proxy}' — check "
                    "~/.ssh/config or the ssh_config of the ssh on PATH)"
                )
            logger.warning("ssh.jump %s", message)
            level = "ERROR"
        else:
            message = f"{name}: connected to {host.get('ssh')} via jump host {jump}"
            logger.info("ssh.jump %s", message)
            level = "INFO"
        try:
            self._emit_ssh_log(
                {"level": level, "logger": "ssh.jump", "message": message, "host": name}
            )
        except Exception:
            logger.debug("could not forward jump probe event", exc_info=True)

    def _report_jump_fallback(
        self, host: dict[str, Any], jump: str, pool_err: str
    ) -> None:
        """One Dev-tab row when a jump host moves from the pooled tunnel to
        system ssh, saying why (the pooled connect's error)."""
        name = str(host.get("name") or "")
        reason = pool_err or "pooled connect failed"
        message = (
            f"{name}: using system ssh through jump host {jump} — the pooled "
            f"tunnel failed ({reason}). Refresh or a host save tries the tunnel again."
        )
        logger.info("ssh.jump %s", message)
        try:
            self._emit_ssh_log(
                {
                    "level": "INFO",
                    "logger": "ssh.jump",
                    "message": message,
                    "host": name,
                }
            )
        except Exception:  # noqa: BLE001 — diagnostics never block the probe
            logger.debug("could not forward jump fallback event", exc_info=True)

    def _check_connected(
        self, host: dict[str, Any], *, ssh_cfg: dict[str, Any] | None = None
    ) -> bool:
        ssh = host.get("ssh")
        if not ssh:
            return True
        if ssh_cfg is None:
            ssh_cfg = self.get_config().get("ssh")
        jump = utils_mod.host_jump(getattr(self, "_hosts", None), host)
        mode = self._jump_mode(host, ssh_cfg) if jump else ""
        if mode == "pool":
            # Pooled tunnel through the jump's warm connection. If it can't be
            # built but system ssh gets through (a legacy target, or a jump only
            # ~/.ssh/config can reach), this host uses system ssh from now on.
            pool = self._ssh_pool
            if pool.connectable(
                ssh, identity_file=host.get("identityFile"), global_ssh_config=ssh_cfg
            ):
                self._report_jump_probe(host, jump, 0, "")
                return True
            pool_err = pool.last_error(
                ssh, identity_file=host.get("identityFile"), global_ssh_config=ssh_cfg
            )
            code, out, err = executor_mod.ssh_jump_run(
                ssh,
                "true",
                jump=jump,
                identity_file=host.get("identityFile"),
                global_ssh_config=ssh_cfg,
                timeout=8,
            )
            if code == 0:
                self._pin_jump_ssh(str(host.get("name") or ""))
                self._report_jump_fallback(host, jump, pool_err)
            self._report_jump_probe(
                host, jump, code, (err or out or pool_err or "").strip()
            )
            return code == 0
        if mode == "ssh":
            # System-ssh transport (not pooled): a successful `ssh … true` over
            # the jump host IS the connectivity check. Uses OpenSSH so legacy
            # algorithms in ~/.ssh/config apply — see executor._system_ssh_argv.
            code, out, err = executor_mod.ssh_jump_run(
                ssh,
                "true",
                jump=jump,
                identity_file=host.get("identityFile"),
                global_ssh_config=ssh_cfg,
                timeout=8,
            )
            self._report_jump_probe(host, jump, code, (err or out or "").strip())
            return code == 0
        pool = self._ssh_pool
        if pool is not None:
            # Probe-free: obtaining a live pooled transport IS the connectivity
            # check, and it warms the connection discovery reuses this cycle —
            # no separate ``ssh host true`` handshake.
            return pool.connectable(
                ssh,
                identity_file=host.get("identityFile"),
                global_ssh_config=ssh_cfg,
            )
        from .executor import ssh_run

        code, _, _ = ssh_run(
            ssh,
            "true",
            identity_file=host.get("identityFile"),
            global_ssh_config=ssh_cfg,
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

    def _get_adapter(
        self, system_override: str | None = None, model_override: str | None = None
    ) -> Any:
        # A system_override (used by trigger turns) builds a one-off adapter with
        # that system prompt — never served from / written to the shared cache.
        # A model_override (a trigger's own `model`) swaps only the model id — same
        # provider, key and settings — and is likewise one-off.
        one_off = system_override is not None or bool(model_override)
        if not one_off and self._adapter is not None:
            return self._adapter
        # AI off-switch: never build an adapter (nor import plugin modules) —
        # every caller treats None as "no model"; the user-facing surfaces word
        # it as AI_OFF_MESSAGE rather than "no provider configured".
        if not getattr(self, "_ai_enabled", True):
            return None
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
        if model_override:
            kwargs["model"] = model_override  # a trigger's own model for this turn
        if llm_cfg.get("aws_region"):
            kwargs["aws_region"] = llm_cfg["aws_region"]
        if llm_cfg.get("base_url"):
            kwargs["base_url"] = llm_cfg["base_url"]
        if llm_cfg.get("api_key_command"):
            # Resolved to the helper's absolute path here, not only inside the
            # built-in adapters' run_api_key_command: a plugin adapter may run the
            # command itself (plain subprocess, the console's own environment),
            # and a console started from a shortcut has no venv Scripts/bin on
            # PATH — so a helper sitting next to the console exe was "not found".
            from .adapters.base import api_key_command_env, prepare_api_key_command

            kwargs["api_key_command"] = prepare_api_key_command(
                str(llm_cfg["api_key_command"]), api_key_command_env()
            )
        if llm_cfg.get("api_key_ttl_ms") is not None:
            kwargs["api_key_ttl_ms"] = int(llm_cfg["api_key_ttl_ms"])
        # Output-token ceiling. The anthropic/bedrock/langserve built-ins accept
        # a max_tokens kwarg; the allowlist below forwards to whatever provider is
        # configured, so guard it (openai would raise TypeError). A plugin adapter
        # declaring max_tokens is auto-forwarded via adapter_config_keys and needs
        # no entry here.
        if llm_cfg.get("max_tokens") is not None and provider in {
            "anthropic",
            "bedrock",
            "langserve",
        }:
            kwargs["max_tokens"] = int(llm_cfg["max_tokens"])
        # Reasoning effort. The anthropic/bedrock built-ins map it to Anthropic
        # adaptive thinking; the langserve built-in passes it through as a wire
        # field (its placement set by effort_key/effort_in below). Guard so a
        # provider that doesn't declare `effort` never sees the kwarg. A plugin
        # adapter declaring `effort` is auto-forwarded via adapter_config_keys
        # below, so it needs no entry here.
        effort = str(llm_cfg.get("effort") or "").strip().lower()
        if effort not in ("", "off", "none") and provider in {
            "anthropic",
            "bedrock",
            "langserve",
        }:
            kwargs["effort"] = effort
        # Built-in `anthropic`-only extras (direct Claude API): 1h prompt cache,
        # refusal fallbacks, thinking summaries. Forwarded only to that provider so
        # bedrock / langserve / plugin adapters never see them.
        if provider == "anthropic":
            for key in ("cache_ttl", "fallbacks", "thinking_display"):
                if str(llm_cfg.get(key) or "").strip():
                    kwargs[key] = str(llm_cfg[key])
        # LangServe-only knobs — describe the gateway's input/output shape and
        # where max_tokens / effort land. Only forwarded when set (and only
        # meaningful to the langserve adapter), so other providers never see them.
        for key in (
            "auth_header",
            "auth_scheme",
            "input_messages_key",
            "input_tools_key",
            "max_tokens_key",
            "max_tokens_in",
            "effort_key",
            "effort_in",
        ):
            if llm_cfg.get(key):
                kwargs[key] = llm_cfg[key]
        if llm_cfg.get("tools_in_config") is not None:
            kwargs["tools_in_config"] = bool(llm_cfg["tools_in_config"])
        # LangServe-only booleans (only meaningful to that adapter; forwarded only
        # when set so no other provider sees them).
        if llm_cfg.get("cache_markers") is not None and provider == "langserve":
            kwargs["cache_markers"] = bool(llm_cfg["cache_markers"])
        if llm_cfg.get("stream_events") is not None and provider == "langserve":
            kwargs["stream_events"] = bool(llm_cfg["stream_events"])
        # [llm.model_limits] — a model-id-substring → capability table the built-in
        # adapters read for capabilities() (context window, output ceiling, whether
        # the gateway accepts thinking/effort). A nested table, so it arrives as a
        # dict under llm_cfg["model_limits"]; forwarded to the built-ins (a plugin
        # adapter declaring the param gets it via adapter_config_keys below).
        if isinstance(llm_cfg.get("model_limits"), dict) and provider in {
            "anthropic",
            "openai",
            "bedrock",
            "langserve",
        }:
            kwargs["model_limits"] = llm_cfg["model_limits"]
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
            if key in kwargs or key in ("provider", "system", "utility_model"):
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
        # Apply the adapter's declared capabilities: clamp the output ceiling to the
        # model's real max, and drop thinking/effort on a gateway that doesn't accept
        # it. Done once per build (adapters are cached) and defensively — a plugin
        # adapter that doesn't implement capabilities() is unaffected.
        self._apply_capabilities(
            adapter, explicit_max_tokens=llm_cfg.get("max_tokens") is not None
        )
        # When a key-vending command is configured, don't cache the adapter — the
        # adapter's own TTL controls refresh. Static-key configs cache as before.
        # A system_override adapter is one-off and never cached.
        if not one_off and not llm_cfg.get("api_key_command"):
            self._adapter = adapter
        return adapter

    def _utility_adapter(self) -> Any:
        """Adapter for the console's own background model calls — conversation
        compaction summaries and the Improve synthesis — which don't need the chat
        model. ``[llm] utility_model`` (e.g. ``"claude-haiku-4-5"``) runs them on a
        cheaper model; unset, they use the ``[llm]`` model as before."""
        try:
            utility = str(self.get_config().get("llm", {}).get("utility_model") or "")
        except Exception:  # noqa: BLE001 — config trouble → the normal adapter
            utility = ""
        if utility.strip():
            return self._get_adapter(model_override=utility.strip())
        return self._get_adapter()

    @staticmethod
    def _apply_capabilities(adapter: Any, explicit_max_tokens: bool = True) -> None:
        """Reconcile an adapter's request settings with its model's capabilities.

        Reads ``adapter.capabilities()`` (best-effort; any adapter may omit it) and:

          * reconciles ``adapter.max_tokens`` with the model's real
            ``max_output_tokens`` — see below; and
          * disables extended thinking / reasoning effort (zeroes the adapter's
            effort field) when ``adaptive_thinking`` is false — a gateway/proxy that
            drops the thinking-effort field would otherwise see a useless (or
            rejected) parameter.

        The reconciliation runs **both ways**, and which way depends on whether the
        operator set ``[llm] max_tokens`` themselves (``explicit_max_tokens``):

          * **Down, always.** A ceiling above the model's real cap 400s, so it is
            clamped whether the operator chose it or not.
          * **Up, only from the adapter's default.** The 8192 default is a
            conservative floor picked to be safe on any model, not a considered
            choice — and leaving it there on a model that allows 64000 makes every
            long reply hit the ceiling and auto-continue, spending extra round
            trips (and re-sending the whole conversation each time) to produce a
            reply the model could have written in one. ``max_tokens`` is a ceiling,
            not a spend — raising it costs nothing unless the tokens are used. An
            operator who set the value explicitly is left alone: a deliberately low
            ceiling is a budget decision, not something to second-guess.

        Never raises into adapter construction."""
        try:
            caps = adapter.capabilities()
        except Exception:  # noqa: BLE001 — capabilities() is optional
            return
        mot = getattr(caps, "max_output_tokens", None)
        cur = getattr(adapter, "max_tokens", None)
        if mot and isinstance(cur, int):
            if cur > int(mot):
                adapter.max_tokens = int(mot)
            elif cur < int(mot) and not explicit_max_tokens:
                adapter.max_tokens = int(mot)
        if not getattr(caps, "adaptive_thinking", True):
            # Built-ins store the normalised effort on _effort (anthropic/bedrock)
            # or effort (langserve); clear whichever is present so no thinking/
            # effort field is sent. A plugin adapter manages its own via its caps.
            if hasattr(adapter, "_effort"):
                adapter._effort = ""
            if hasattr(adapter, "effort"):
                adapter.effort = ""

    # ── event triggers ────────────────────────────────────────────────────────

    def _now_context(self) -> str:
        """A per-turn system-prompt block giving the agent the operator's current
        local date **and timezone**, so it resolves relative dates ("last Friday")
        against the real machine clock and produces clock times in local time
        instead of guessing from its training cutoff or defaulting to UTC. Folded
        into every agent turn (chat, trigger, schedule).

        Date + timezone, but **no clock time** on purpose: date and timezone both
        change at most once a day (the tz only at a DST boundary), so the composed
        system prompt stays byte-identical across a day's turns and the model-side
        prompt cache keeps hitting — a per-minute timestamp would miss the cache
        every turn. The exact current *time* is deliberately left to the on-demand
        ``console_status`` tool. Local — ``datetime.now().astimezone()`` — because
        "last Friday" and "2pm" are the operator's local calendar/clock."""
        now = datetime.now().astimezone()
        tzname = now.strftime("%Z")
        raw = now.strftime("%z")  # e.g. -0500 / +0530 / "" if unknown
        offset = f"UTC{raw[:3]}:{raw[3:]}" if len(raw) == 5 else "UTC offset unknown"
        tz = f"{tzname} ({offset})" if tzname else offset
        return (
            "## Current date & timezone\n"
            f"The operator's machine date is **{now.strftime('%A, %Y-%m-%d')}** and "
            f"their timezone is **{tz}** (local time). Resolve relative dates like "
            '"last Friday" or "next week" against this date. Produce and interpret '
            "clock times in the operator's **local** time — never UTC (e.g. a 2pm "
            "meeting is 14:00 local). For the exact current time — needed for "
            'relative times like "in 30 minutes" or "this afternoon" — call the '
            "``console_status`` tool first."
        )

    def default_system_prompt(self) -> str:
        """The built-in base system prompt (what runs when ``[llm] system`` is
        unset) — read by Settings → Model / API to pre-fill the field."""
        return DEFAULT_SYSTEM_PROMPT

    def _base_system(self) -> str:
        """Base system prompt for the chat + trigger agent turns: the configured
        assistant instructions (``[llm] system``) if set, else a sensible default,
        with ``[llm] system_append`` appended to whichever won.
        compose_system() layers the per-trigger rules on top of this.

        ``[llm] system`` still **replaces** the default wholesale — including the
        clarify-don't-guess guideline — because changing that would silently alter
        behaviour for every operator who already relies on a custom prompt. That
        replacement is exactly why ``system_append`` exists: it adds site policy on
        top of the default instead of trading one for the other, which is the
        combination most operators actually want."""
        llm_cfg = self.get_config().get("llm", {})
        configured = str(llm_cfg.get("system", "")).strip()
        base = configured or DEFAULT_SYSTEM_PROMPT
        append = str(llm_cfg.get("system_append", "")).strip()
        return f"{base}\n\n{append}" if append else base

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
        try:
            from . import savedruns as savedruns_mod

            saved_runs = [r for r in savedruns_mod.load_saved_runs() if r.get("id")]
        except Exception:
            saved_runs = []
        rota_on = bool(rota.get("enabled"))
        _mcp_status = getattr(self, "mcp_status", None)
        mcp_servers = (
            [s for s in _mcp_status().get("servers", []) if s.get("connected")]
            if callable(_mcp_status)
            else []
        )
        if (
            not rota_on
            and not triggers
            and not schedules
            and not mailbox
            and not host_groups
            and not saved_runs
            and not mcp_servers
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
                elif action == "saved_run":
                    target = f"saved run {s.get('saved_run')}"
                elif action == "saved_transfer":
                    target = f"saved transfer {s.get('saved_transfer')}"
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
                "check or do something later), action='runbook' (run a saved "
                "runbook on a cadence), and action='saved_run' (fire a saved run "
                "— a deterministic preset — on a cadence)."
            )

        if saved_runs:
            lines.append("")
            lines.append(f"Saved runs ({len(saved_runs)}) — deterministic presets:")
            for r in saved_runs[:20]:
                try:
                    parsed = savedruns_mod.parse_saved_run(r)
                    target = parsed.target_summary()
                except Exception:
                    target = "?"
                params = sorted((r.get("params") or {}).keys())
                extra = f" (params: {', '.join(params)})" if params else ""
                title = f" — {r.get('title')}" if r.get("title") else ""
                lines.append(
                    f"- {r.get('id')}{title}: /{r.get('runnable')} on {target}{extra}"
                )
            if len(saved_runs) > 20:
                lines.append(f"- … and {len(saved_runs) - 20} more")
            lines.append(
                "Fire one with the run_saved_run tool (fill open params via "
                "'values'); it runs exactly as stored — the gate reflects the "
                "underlying runnable's autonomy. Manage them with "
                "manage_saved_runs."
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

        if mcp_servers:
            lines.append("")
            summary = ", ".join(
                f"{s['name']} ({s['tool_count']} tool"
                f"{'' if s['tool_count'] == 1 else 's'})"
                for s in mcp_servers
            )
            lines.append(
                f"External MCP servers connected: {summary}. Their tools are named "
                "`mcp_<server>_<tool>`. When one is in your tool list, call it like "
                "any other tool; when it isn't, it's in the deferred-tools menu — "
                "get its schema with describe_tool and run it with use_tool. They "
                "run on an outside server, so most are confirm-gated unless the "
                "operator marked the server trusted."
            )

        # One-liner about the per-session workspace so the agent reaches for the
        # workspace_* tools when a spilled envelope shows up. Deliberately
        # id-free (the envelope tells you the file path each time) so the
        # cached system prefix stays stable across turns of one conversation.
        guidance = self._workspace_guidance()
        if guidance:
            lines.append("")
            lines.append(guidance)
        from . import sandbox

        if (
            config_mod.read_workspace().get("code_exec", True)
            and config_mod.read_sandbox().get("enabled", True)
            and sandbox.available()
        ):
            lines.append(
                "You can also process what's in the workspace inside a secure WASM "
                "sandbox (no network, no host access — only this workspace): "
                "run_python_sandboxed runs a Python snippet with the workspace as "
                "its working dir (read a spilled file, reshape/join data, compute, "
                "print; stdlib incl. sqlite3, but not the console's own installed "
                "packages) — it asks the operator to approve first; "
                "run_sql_sandboxed queries a SQLite file in the workspace "
                "(read-only queries run without asking). Prefer these over doing "
                "heavy data-wrangling by hand."
            )

        return "\n".join(lines)

    def _workspace_guidance(self) -> str:
        """The standing session-workspace block folded into **every** agent turn
        (chat, trigger, schedule, portal runbook) and into the ``console_status``
        snapshot: what a spilled envelope is, which ``workspace_*`` tools read it
        back, when to set a runnable call's ``deliver``, and that the read-only
        reshaping tools (``workspace_json_get`` / ``workspace_materialize``) come
        before the confirm-gated ``run_python_sandboxed``.

        Static text — no ids, no dates — so it is byte-identical across turns and
        the model-side prompt cache still hits. Returns ``""`` when spill is
        disabled (``[workspace] spill_threshold = 0``): with nothing ever spilled
        the advice would only mislead. Lives here (not in the default system
        prompt) so a configured ``[llm] system`` doesn't drop it — the tools it
        names are offered regardless of the persona."""
        try:
            if not config_mod.read_workspace().get("spill_threshold", 0):
                return ""
        except (OSError, ValueError, TypeError):
            return ""
        return WORKSPACE_GUIDANCE

    def _presence_caveat(self) -> str:
        """The lean per-turn block folded into interactive **chat** turns, in
        place of the full ``_console_context()`` inventory.

        Returns ``""`` on the common turn — operator present, inside working
        hours — so the standing system prompt (and the model-side prompt cache)
        stays clean. It emits a single "## Working hours" caveat **only** when
        the operator is interacting *outside* their configured working hours,
        where the advice ("this isn't blocked, but confirm before side effects")
        actually shapes behaviour. Everything else the old block carried — the
        lock-suspend / break lines and the triggers/schedules/groups/mailbox/
        repos inventory — is now pulled on demand from the ``console_status``
        tool (which still returns the full ``_console_context()`` snapshot), so
        nothing is lost; it just no longer rides every turn.

        Reactive trigger/schedule turns never took this path at all: a firing
        while the operator is away is dropped by the engine's rota/lock gate
        before any LLM turn starts, so presence never needed to be a standing
        prompt concern. Uses ``within_rota`` (the grid), not a live calendar
        probe, to stay latency-free — matching how ``_console_context()``
        computes its INSIDE/OUTSIDE display."""
        from .config import read_rota
        from .triggers import within_rota

        rota = read_rota()
        if not rota.get("enabled"):
            return ""
        if within_rota(rota, datetime.now()):
            return ""  # present and inside working hours — nothing to say
        note = (
            " (the live gate also consults your Outlook calendar, so treat this "
            "as approximate)"
            if rota.get("source") == "outlook"
            else ""
        )
        return (
            "## Working hours\n"
            "It is currently **OUTSIDE** the operator's configured working "
            f"hours{note}. This interactive request is not blocked, but if it "
            "asks you to run, schedule, or trigger something with side effects, "
            "point out that it's outside their working hours and confirm they "
            "want to proceed before acting."
        )
