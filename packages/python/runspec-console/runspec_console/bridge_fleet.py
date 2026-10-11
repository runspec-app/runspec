"""bridge_fleet.py — FleetMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import hosts as hosts_mod
from .config import (
    backups_keep_value,
    filters_path,
    read_sync,
    write_sync,
)

from .bridge_const import (
    logger,
)
from .bridge_utils import (
    _normalize_path,
    _clean_str_list,
    _parse_ssh,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


# A package config seed may carry [config_sync] to bootstrap the live layer, but
# must not clobber the machine-local run-state a git pull stamps. These paths are
# pinned to the local value during a seed apply (on top of DEFAULT_PRESERVE).
_SEED_PRESERVE: tuple[str, ...] = (
    "config.toml:config_sync.last_ref",
    "config.toml:config_sync.last_commit",
    "config.toml:config_sync.last_synced",
)


class FleetMixin:
    def test_host(self, name: str) -> dict[str, Any]:
        entry = self._host_entry(name)
        if entry is None:
            return {
                "connected": False,
                "runspec_ok": False,
                "runnable_count": 0,
                "stdout": "",
                "stderr": f"Host '{name}' not found in hosts file",
                "exit_code": -1,
            }

        ssh = entry.get("ssh")
        paths = utils_mod._paths(entry)
        rp = paths[0] if paths else ""
        idf = entry.get("identityFile")

        if ssh:
            code, out, err = self._ssh_run(
                ssh, f"{rp} local --format json", identity_file=idf
            )
            connected = code != -1  # -1 means connection itself failed
            ok = code == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(out))
                except Exception:
                    ok = False
            return {
                "connected": connected,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": out[:2000],
                "stderr": err[:2000],
                "exit_code": code,
            }
        else:
            import subprocess as _sp

            try:
                r = _sp.run(
                    [rp, "local", "--format", "json"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:
                return {
                    "connected": False,
                    "runspec_ok": False,
                    "runnable_count": 0,
                    "stdout": "",
                    "stderr": str(exc),
                    "exit_code": -1,
                }
            ok = r.returncode == 0
            count = 0
            if ok:
                try:
                    import json as _json

                    count = len(_json.loads(r.stdout))
                except Exception:
                    ok = False
            return {
                "connected": True,
                "runspec_ok": ok,
                "runnable_count": count,
                "stdout": r.stdout[:2000],
                "stderr": r.stderr[:2000],
                "exit_code": r.returncode,
            }

    def get_jump_hosts(self) -> list[dict[str, Any]]:
        self._reload_hosts()
        result: list[dict[str, Any]] = []
        for h in self._hosts:
            ssh = h.get("ssh", "")
            user, hostname, port = _parse_ssh(ssh)
            entry: dict[str, Any] = {
                "name": h["name"],
                "hostname": hostname,
                "runspec_paths": utils_mod._paths(h),
            }
            if user:
                entry["user"] = user
            if port is not None:
                entry["port"] = port
            if h.get("identityFile"):
                entry["identityFile"] = h["identityFile"]
            if h.get("group"):
                entry["group"] = h["group"]
            if str(h.get("jump") or "").strip():
                entry["jump"] = str(h["jump"]).strip()
            # Host-profile (working-set) tags — the editor round-trips the full
            # host list on save (save_jump_hosts rebuilds every managed key), so
            # omitting ``profiles`` here made a save wipe every host's tags.
            prof = hosts_mod.host_profiles(h)
            if prof:
                entry["profiles"] = prof
            if h.get("room"):
                entry["room"] = h["room"]
            if isinstance(h.get("room_port"), int) and not isinstance(
                h.get("room_port"), bool
            ):
                entry["room_port"] = h["room_port"]
            if h.get("room_user"):
                entry["room_user"] = h["room_user"]
            if str(h.get("room_via") or "").strip().lower() == "exec":
                entry["room_via"] = "exec"
            if h.get("room_exec_command"):
                entry["room_exec_command"] = h["room_exec_command"]
            if h.get("room_queue"):
                entry["room_queue"] = h["room_queue"]
            if isinstance(h.get("room_catalog"), list):
                names = [str(n).strip() for n in h["room_catalog"] if str(n).strip()]
                if names:
                    entry["room_catalog"] = names
            result.append(entry)
        return result

    # Host-entry keys the Hosts editor owns (and rebuilds on save). Everything
    # else in an entry — e.g. room_dest_host, role, or a hand-added key — is
    # preserved verbatim so a save through the UI never silently drops it.
    _MANAGED_HOST_KEYS = frozenset(
        {
            "name",
            "ssh",
            "hostname",
            "user",
            "port",
            "runspec_paths",
            "runspec_path",
            "identityFile",
            "group",
            "jump",
            "profiles",
            "room",
            "room_port",
            "room_user",
            "room_via",
            "room_exec_command",
            "room_queue",
            "room_catalog",
        }
    )

    def save_jump_hosts(self, hosts: list[dict[str, Any]]) -> None:
        # Freshest on-disk entries, to carry over any keys the editor doesn't
        # manage. load_hosts is tolerant of a corrupt file (it quarantines and
        # returns []), so a previously-broken file can't wedge a save.
        existing = {
            str(e.get("name") or ""): e
            for e in hosts_mod.load_hosts(config_mod.hosts_path())
        }
        entries: list[dict[str, Any]] = []
        for h in hosts:
            hostname = h.get("hostname", "")
            user = h.get("user", "")
            port = h.get("port")
            ssh = f"{user}@{hostname}" if user else hostname
            if port:
                ssh = f"{ssh}:{port}"
            raw_paths = h.get("runspec_paths") or []
            if not raw_paths and h.get("runspec_path"):
                raw_paths = [h["runspec_path"]]
            # Drop blank / None rows before normalising: an empty "Add path" row in
            # the editor arrives as null over the bridge, and a bare [null] used to
            # be written verbatim, producing invalid TOML that wedged every later
            # save. Keep only real, non-empty path strings.
            raw_paths = [p for p in raw_paths if isinstance(p, str) and p.strip()]
            # Normalise filesystem paths to forward slashes for TOML storage.
            # Remote runspec paths are POSIX so this is a no-op for them;
            # identityFile on Windows benefits from the conversion.
            normalised_paths = [_normalize_path(p) for p in raw_paths]
            entry: dict[str, Any] = {
                "name": h["name"],
                "ssh": ssh,
                "runspec_paths": normalised_paths or ["runspec"],
            }
            if h.get("identityFile"):
                entry["identityFile"] = _normalize_path(h["identityFile"])
            if h.get("group"):
                entry["group"] = h["group"]
            # System-ssh ProxyJump target — reach this host through the jump
            # host's OpenSSH so legacy ssh-rsa/ssh-dss handshakes work.
            if str(h.get("jump") or "").strip():
                entry["jump"] = str(h["jump"]).strip()
            # Host-profile (working-set) tags — which profiles load this host.
            profiles = h.get("profiles")
            if isinstance(profiles, list):
                names = [str(p).strip() for p in profiles if str(p).strip()]
                if names:
                    entry["profiles"] = names
            # The room server on this fleet host: ``room_port`` alone is enough to
            # staff the helpdesk (the queue defaults server-side; no ``room_queue``
            # needed). A group-chat ``room`` and/or an explicit ``room_queue`` are
            # optional. All ride this host's ssh target + key (see _room_specs /
            # host_rooms); the transport keys (user/via/exec command) are shared.
            room = str(h.get("room") or "").strip()
            queue = str(h.get("room_queue") or "").strip()
            room_port = h.get("room_port")
            has_port = isinstance(room_port, int) and not isinstance(room_port, bool)
            if has_port:
                entry["room_port"] = room_port
                if room:
                    entry["room"] = room
                if queue:
                    entry["room_queue"] = queue
                    # Optional per-queue catalog allowlist (bare runnable names).
                    catalog = h.get("room_catalog")
                    if isinstance(catalog, list):
                        names = [str(n).strip() for n in catalog if str(n).strip()]
                        if names:
                            entry["room_catalog"] = names
                if str(h.get("room_user") or "").strip():
                    entry["room_user"] = h["room_user"].strip()
                # Exec-channel transport (for networks that block port-forwarding).
                if str(h.get("room_via") or "").strip().lower() == "exec":
                    entry["room_via"] = "exec"
                if str(h.get("room_exec_command") or "").strip():
                    entry["room_exec_command"] = h["room_exec_command"].strip()
            # Carry over any unmanaged keys from the existing entry (room_dest_host,
            # role, …) so they survive an editor save.
            for k, v in existing.get(str(h.get("name") or ""), {}).items():
                if k not in self._MANAGED_HOST_KEYS and k not in entry:
                    entry[k] = v
            entries.append(entry)
        hosts_mod.save_hosts(config_mod.hosts_path(), entries)
        self._reload_hosts()
        self.restart_chat()  # re-join rooms with the updated host config
        # Signal host-list consumers (sidebar, the Self-Service room selector) to
        # refetch — otherwise e.g. a host whose room config was just removed still
        # shows in the room dropdown until the app restarts.
        self._dispatch("runspec:hosts_updated", {})
        # An edit is a fresh attempt: a host backing off after failed connects
        # would otherwise wait out its window before the new settings are tried.
        pool = getattr(self, "_ssh_pool", None)
        if pool is not None:
            pool.clear_backoff()
        self._kick_refresh(force=True)

    # ── local venv auto-discovery ([local] venv_globs) ────────────────────────

    def get_local_venv_config(self) -> dict[str, Any]:
        """The local machine's venv auto-discovery config (Settings → Local venvs).

        Returns the saved ``[local] venv_globs`` convention **and** a live preview
        of what it resolves to on *this* machine:
        ``{globs, own_venv, resolved: [{path, venv_name}]}``. ``own_venv`` (always
        present, first) is the console's own venv; ``resolved`` is every local venv
        currently discovered (own + glob matches). The globs ride Config Sync / the
        config seed (config.toml); ``resolved`` is machine-local — what actually
        exists here — so the editor can show that honestly."""
        globs = config_mod.read_local().get("venv_globs") or []
        paths = utils_mod._paths(self._local_host_entry())
        resolved = [{"path": p, "venv_name": hosts_mod.venv_name(p)} for p in paths]
        return {
            "globs": list(globs),
            "own_venv": paths[0] if paths else "",
            "resolved": resolved,
        }

    def save_local_venv_config(self, globs: list[str]) -> dict[str, Any]:
        """Persist the ``[local] venv_globs`` convention, then rediscover.

        Validates ``globs`` is a list of non-empty strings (raising ``ValueError``
        to reject the JS promise so the tab can show the message), writes it into
        config.toml via :func:`config.update_config` (atomic, preserves every other
        section), then signals host-list consumers and kicks a refresh cycle so the
        new venvs appear without a restart. Returns the fresh
        :meth:`get_local_venv_config`."""
        if not isinstance(globs, list) or any(
            not isinstance(g, str) or not g.strip() for g in globs
        ):
            raise ValueError("venv_globs must be a list of non-empty strings")
        cleaned = [g.strip() for g in globs]

        def _mutate(cfg: dict[str, Any]) -> None:
            local = cfg.get("local")
            if not isinstance(local, dict):
                local = {}
            if cleaned:
                local["venv_globs"] = cleaned
            else:
                local.pop("venv_globs", None)
            if local:
                cfg["local"] = local
            else:
                cfg.pop("local", None)

        config_mod.update_config(_mutate)
        self._dispatch("runspec:hosts_updated", {})
        self._kick_refresh(force=True)
        return self.get_local_venv_config()

    # ── host-group registry (runspec_groups.toml) ─────────────────────────────

    def get_groups(self) -> list[dict[str, Any]]:
        """The canonical host-group registry — ``[{name, description}]`` in
        registry order — that the host editor's group dropdown is populated from.

        Self-healing: any group a host currently references that isn't registered
        yet is **absorbed** into the registry (and persisted, so it syncs). This
        migrates pre-registry free-form groups and guarantees the dropdown always
        offers every in-use group."""
        from .config import groups_path
        from .groups import load_groups, normalise_groups
        from .groups import save_groups as _save

        registry = normalise_groups(load_groups(groups_path()))
        known = {g["name"].casefold() for g in registry}
        added = False
        for entry in self._all_host_entries():
            g = str(entry.get("group") or "").strip()
            if g and g.casefold() not in known:
                registry.append({"name": g, "description": ""})
                known.add(g.casefold())
                added = True
        if added:
            _save(groups_path(), registry)
        return registry

    def save_groups(self, groups: list[dict[str, Any]]) -> None:
        """Replace the registry (add / edit description / delete / reorder). On a
        **delete**, any host whose group is no longer registered is reset to
        Ungrouped so membership never dangles. Use :meth:`rename_group` to rename
        (it preserves membership; a delete+add here would orphan it)."""
        from .config import groups_path
        from .groups import normalise_groups
        from .groups import save_groups as _save

        norm = normalise_groups(groups)
        _save(groups_path(), norm)
        legal = {g["name"].casefold() for g in norm}
        self._reload_hosts()
        changed = False
        for h in self._hosts:
            g = str(h.get("group") or "").strip()
            if g and g.casefold() not in legal:
                h.pop("group", None)
                changed = True
        if changed:
            hosts_mod.save_hosts(config_mod.hosts_path(), self._hosts)
            self._reload_hosts()
        self._dispatch("runspec:hosts_updated", {})

    def rename_group(self, old: str, new: str) -> None:
        """Rename a group in the registry AND re-point every host assigned to it,
        so membership is preserved across the rename."""
        old_s = str(old or "").strip()
        new_s = str(new or "").strip()
        if not old_s or not new_s:
            raise ValueError("rename needs both the old and new group name")
        from .config import groups_path
        from .groups import load_groups, normalise_groups
        from .groups import save_groups as _save

        registry = load_groups(groups_path())
        for g in registry:
            if str(g.get("name") or "").strip().casefold() == old_s.casefold():
                g["name"] = new_s
        _save(groups_path(), normalise_groups(registry))
        self._reload_hosts()
        changed = False
        for h in self._hosts:
            if str(h.get("group") or "").strip().casefold() == old_s.casefold():
                h["group"] = new_s
                changed = True
        if changed:
            hosts_mod.save_hosts(config_mod.hosts_path(), self._hosts)
            self._reload_hosts()
        self._dispatch("runspec:hosts_updated", {})

    # ── host-profile registry (runspec_profiles.toml) ─────────────────────────

    def get_profiles(self) -> list[dict[str, Any]]:
        """The canonical host-profile registry — ``[{name, description}]`` in
        registry order — that the profile switcher and per-host profile picker are
        populated from. See ``docs/design/host-profiles.md``.

        Self-healing (like :meth:`get_groups`): any profile a host's ``profiles``
        tag references that isn't registered yet is **absorbed** into the registry
        (and persisted, so it syncs), so the picker always offers every in-use
        profile and pre-registry tags migrate automatically."""
        from .config import profiles_path
        from .profiles import load_profiles, normalise_profiles
        from .profiles import save_profiles as _save

        registry = normalise_profiles(load_profiles(profiles_path()))
        known = {p["name"].casefold() for p in registry}
        added = False
        for entry in self._all_host_entries():
            for tag in hosts_mod.host_profiles(entry):
                if tag.casefold() not in known:
                    registry.append({"name": tag, "description": ""})
                    known.add(tag.casefold())
                    added = True
        if added:
            _save(profiles_path(), registry)
        return registry

    def save_profiles(self, profiles: list[dict[str, Any]]) -> None:
        """Replace the registry (add / edit description / delete / reorder). On a
        **delete**, the now-illegal tag is stripped from every host's ``profiles``
        array so membership never dangles. Use :meth:`rename_profile` to rename
        (a delete+add here would orphan membership). If the deleted profile was the
        active one, the active pointer falls back to *All hosts*."""
        from .config import active_profile_path, profiles_path
        from .profiles import normalise_profiles, read_active_profile
        from .profiles import save_profiles as _save
        from .profiles import write_active_profile

        norm = normalise_profiles(profiles)
        _save(profiles_path(), norm)
        legal = {p["name"].casefold() for p in norm}
        self._reload_hosts()
        changed = False
        for h in self._hosts:
            tags = hosts_mod.host_profiles(h)
            kept = [t for t in tags if t.casefold() in legal]
            if kept != tags:
                if kept:
                    h["profiles"] = kept
                else:
                    h.pop("profiles", None)
                changed = True
        if changed:
            hosts_mod.save_hosts(config_mod.hosts_path(), self._hosts)
            self._reload_hosts()
        # If the active profile was just deleted, drop back to All hosts.
        active = read_active_profile(active_profile_path())
        if active and active.casefold() not in legal:
            write_active_profile(active_profile_path(), "")
            self._kick_refresh(force=True)
        self._dispatch("runspec:hosts_updated", {})

    def rename_profile(self, old: str, new: str) -> None:
        """Rename a profile in the registry AND re-point every host's ``profiles``
        tag (and the active pointer if it named the old profile), so membership is
        preserved across the rename."""
        old_s = str(old or "").strip()
        new_s = str(new or "").strip()
        if not old_s or not new_s:
            raise ValueError("rename needs both the old and new profile name")
        from .config import active_profile_path, profiles_path
        from .profiles import load_profiles, normalise_profiles, read_active_profile
        from .profiles import save_profiles as _save
        from .profiles import write_active_profile

        registry = load_profiles(profiles_path())
        for p in registry:
            if str(p.get("name") or "").strip().casefold() == old_s.casefold():
                p["name"] = new_s
        _save(profiles_path(), normalise_profiles(registry))
        self._reload_hosts()
        changed = False
        for h in self._hosts:
            tags = hosts_mod.host_profiles(h)
            repointed = [new_s if t.casefold() == old_s.casefold() else t for t in tags]
            if repointed != tags:
                h["profiles"] = repointed
                changed = True
        if changed:
            hosts_mod.save_hosts(config_mod.hosts_path(), self._hosts)
            self._reload_hosts()
        if read_active_profile(active_profile_path()).casefold() == old_s.casefold():
            write_active_profile(active_profile_path(), new_s)
        self._dispatch("runspec:hosts_updated", {})

    def get_active_profile(self) -> str:
        """The machine-local active host profile (``""`` = *All hosts*)."""
        return self._active_profile_name()

    def set_active_profile(self, name: str) -> dict[str, Any]:
        """Switch the active host profile (``""`` = *All hosts*) — the "reboot into
        a scope" action. Hosts leaving scope have their pooled SSH transport
        evicted immediately and their connected dot cleared; a refresh cycle then
        probes/discovers the newly in-scope hosts. Machine-local — never synced."""
        from .config import active_profile_path
        from .profiles import write_active_profile

        before = {h["name"] for h in self._active_host_entries() if h.get("ssh")}
        write_active_profile(active_profile_path(), name)
        after_entries = self._active_host_entries()
        after = {h["name"] for h in after_entries if h.get("ssh")}
        leaving = before - after

        pool = self._ssh_pool
        if leaving:
            global_ssh = self.get_config().get("ssh") or {}
            by_name = {h.get("name"): h for h in self._all_host_entries()}
            for n in leaving:
                h = by_name.get(n)
                if pool is not None and h and h.get("ssh"):
                    try:
                        pool.drop(
                            h["ssh"],
                            identity_file=h.get("identityFile"),
                            global_ssh_config=global_ssh,
                        )
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        pass
            with self._lock:
                for n in leaving:
                    self._connected_cache.pop(n, None)

        self._dispatch("runspec:hosts_updated", {})
        self._kick_refresh(force=True)
        return {"active": str(name or "").strip()}

    # ── self-service catalog registry (runspec_self_service.toml) ──────────────

    def get_self_service(self) -> list[dict[str, Any]]:
        """The self-service catalog registry — ``[{name, description}]`` in registry
        order — naming which discovered runnables are offered to *visitors* in the
        helpdesk room. This replaces the runspec.toml ``discoverable =
        ["self-service"]`` marking (a front-end concern, so it lives here); the
        Settings → Self-Service tab edits it. Rides Config Sync, merged by name."""
        from .config import self_service_path
        from .self_service import load_self_service, normalise_self_service

        return normalise_self_service(load_self_service(self_service_path()))

    def save_self_service(self, entries: list[dict[str, Any]]) -> None:
        """Replace the self-service registry (add / remove / edit description /
        reorder) and republish every connected queue's catalog so the room reflects
        the change immediately."""
        from .config import self_service_path
        from .self_service import save_self_service as _save

        _save(self_service_path(), entries)
        try:
            self._publish_catalogs()
        except Exception:
            logger.debug("republish after save_self_service failed", exc_info=True)
        self._dispatch("runspec:self_service_changed", {})

    def get_repos(self) -> list[dict[str, Any]]:
        """The git-repo registry (``runspec_repos.toml``) for Settings → Repos —
        each entry plus its local-checkout status (``checked_out`` + short
        ``commit``), its machine-local custom checkout ``path`` ("" = default
        AppData location), and the effective ``checkout_dir``. The registry is the
        agent's allowlist: a repo not listed here can't be cloned/read/edited by
        the ``repo_*`` tools."""
        from . import repos as repos_mod

        out: list[dict[str, Any]] = []
        for r in repos_mod.read_repos():
            name = r["name"]
            out.append(
                {
                    **r,
                    "path": repos_mod.custom_path(name),
                    "checkout_dir": str(repos_mod.checkout_dir(name)),
                    "checked_out": repos_mod.is_checked_out(name),
                    "commit": repos_mod.head_commit(name),
                }
            )
        return out

    def save_repos(self, repos: list[dict[str, Any]]) -> None:
        """Replace the repo registry (add / edit / reorder). A repo's optional
        custom checkout ``path`` is **machine-local** — it's split out to its own
        ``runspec_repo_paths.toml`` (never synced) and stripped from the synced
        registry. Local checkouts on disk are left alone here; deletion goes
        through :meth:`delete_repo`."""
        from . import repos as repos_mod
        from .config import repo_paths_path, repos_path

        # Rebuild the whole override map from the saved set, so an override for a
        # repo no longer present is also dropped.
        mapping: dict[str, str] = {}
        for r in repos or []:
            name = str(r.get("name") or "").strip()
            path = str(r.get("path") or "").strip()
            if name and path:
                mapping[name] = path
        repos_mod.save_repo_paths(repo_paths_path(), mapping)
        repos_mod.save_repos(repos_path(), repos)

    def delete_repo(self, name: str) -> dict[str, Any]:
        """Remove a repo from the registry **and** delete its AppData checkout from
        disk (Settings → Repos 'Delete'). A custom dev-folder checkout is **never**
        touched — the operator owns those working copies. Returns
        ``{ok, removed_checkout}``."""
        from . import repos as repos_mod
        from .config import repos_path

        key = str(name or "").strip().casefold()
        kept = [r for r in repos_mod.read_repos() if r["name"].casefold() != key]
        removed = repos_mod.delete_checkout(name)
        repos_mod.save_repos(repos_path(), kept)
        return {"ok": True, "removed_checkout": removed}

    def sync_repo(self, name: str) -> dict[str, Any]:
        """Operator-initiated clone/fetch of a registered repo (Settings → Repos
        'Sync now'). The operator's click is the authorization, so this runs
        directly — unlike the agent's confirm-gated ``repo_sync`` tool. Returns
        ``{ok, action?, commit?, error?}``; never raises (errors ride the dict)."""
        from . import repos as repos_mod

        entry = repos_mod.find_repo(name)
        if entry is None:
            return {"ok": False, "error": f"no repo named {name!r} is registered"}
        try:
            auth = self._sync_auth(entry["url"], entry.get("credential_id", ""))
            res = repos_mod.sync_repo(
                entry["url"],
                entry["ref"],
                auth,
                repos_mod.checkout_dir(entry["name"]),
            )
            return {"ok": True, "action": res["action"], "commit": res["commit"][:10]}
        except Exception as exc:  # noqa: BLE001 — surface git failures to the UI
            return {"ok": False, "error": str(exc)}

    def get_filters(self) -> list[dict[str, Any]]:
        """Named source-side stream filters, for the Settings tab and pickers."""
        self._reload_filters()
        result: list[dict[str, Any]] = []
        for f in self._filters:
            entry: dict[str, Any] = {
                "name": f.get("name", ""),
                "pipeline": f.get("pipeline", ""),
            }
            if f.get("description"):
                entry["description"] = f["description"]
            if f.get("param_label"):
                entry["param_label"] = f["param_label"]
            result.append(entry)
        return result

    def save_filters(self, filters: list[dict[str, Any]]) -> None:
        from .filters_store import save_filters as _save_filters

        entries: list[dict[str, Any]] = []
        for f in filters:
            name = str(f.get("name", "")).strip()
            pipeline = str(f.get("pipeline", "")).strip()
            if not name or not pipeline:
                continue  # both are required; drop incomplete rows
            entry: dict[str, Any] = {"name": name, "pipeline": pipeline}
            if f.get("description"):
                entry["description"] = str(f["description"]).strip()
            if f.get("param_label"):
                entry["param_label"] = str(f["param_label"]).strip()
            entries.append(entry)
        _save_filters(filters_path(), entries)
        self._reload_filters()

    def import_filters(self, toml_content: str) -> list[dict[str, Any]]:
        """Merge filters from an exported ``runspec_filters.toml`` blob, skipping
        names that already exist. Returns the newly-added filters."""
        from .filters_store import save_filters as _save_filters

        try:
            data = tomllib.loads(toml_content)
        except Exception:
            return []
        imported = data.get("filter", [])
        existing = {f.get("name") for f in self._filters}
        new = [
            f
            for f in imported
            if f.get("name") and f.get("pipeline") and f["name"] not in existing
        ]
        if new:
            _save_filters(filters_path(), [*self._filters, *new])
            self._reload_filters()
        return new

    # ── credentials ─────────────────────────────────────────────────────────────

    def credentials_backend(self) -> dict[str, Any]:
        """Whether secrets can be stored, and which OS keyring backs them.

        ``available`` False means the ``credentials`` extra isn't installed or no
        usable OS store resolved — the UI warns and disables saving secrets.
        """
        from . import credentials as cred

        name = cred.backend_name()
        return {"available": name is not None, "backend": name or ""}

    def list_credentials(self) -> list[dict[str, Any]]:
        """Saved credentials' **metadata** for the Settings tab — never the secret.

        Each entry carries ``has_secret`` (whether a value is stored in the
        keyring) so the UI can show set/not-set without revealing anything."""
        from . import credentials as cred

        self._reload_credentials()
        result: list[dict[str, Any]] = []
        for c in self._credentials:
            cid = str(c.get("id", ""))
            entry: dict[str, Any] = {
                "id": cid,
                "label": c.get("label", ""),
                "kind": c.get("kind", "userpass"),
                "username": c.get("username", ""),
                "username_arg": c.get("username_arg", ""),
                "secret_arg": c.get("secret_arg", ""),
                "private_key": c.get("private_key", ""),
                "public_key": c.get("public_key", ""),
                "scope_hosts": list(c.get("scope_hosts", []) or []),
                "scope_runnables": list(c.get("scope_runnables", []) or []),
                "has_secret": cred.has_secret(cid) if cid else False,
            }
            result.append(entry)
        return result

    def credentials_for_runnable(
        self, host: str, runnable: str
    ) -> list[dict[str, Any]]:
        """In-scope credentials for ``(host, runnable)`` — id + label only — so the
        Forms run dialog can offer them as a pick/override. (In-scope credentials
        auto-apply on run; choosing one here pins it.)"""
        from . import credentials as cred

        self._reload_credentials()
        return [
            {
                "id": str(c.get("id", "")),
                "label": c.get("label", ""),
                "has_secret": cred.has_secret(str(c.get("id", ""))),
                # The runnable args this credential fills, so the form can treat a
                # required arg as satisfied (and not block) when a credential covers
                # it. username is non-secret metadata; the secret itself never leaves
                # the keyring — has_secret says whether secret_arg will be filled.
                "secret_arg": str(c.get("secret_arg", "")).strip(),
                "username_arg": str(c.get("username_arg", "")).strip(),
                "username": str(c.get("username", "")).strip(),
            }
            for c in self._scoped_credentials(host, runnable)
        ]

    def save_credential(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Create or update one credential (upsert by ``id``).

        Metadata is written to ``runspec_credentials.toml``; a non-empty
        ``secret`` is stored in the OS keyring (and stripped from metadata). A
        blank/absent ``secret`` leaves any existing stored secret untouched —
        the field is write-only, so editing other metadata never clears it.
        Returns ``{"id": ...}`` or ``{"error": ...}``.
        """
        from . import credentials as cred
        from .credentials import VALID_KINDS, save_credentials

        label = str(entry.get("label", "")).strip()
        if not label:
            return {"error": "a label is required"}
        kind = str(entry.get("kind", "userpass")).strip() or "userpass"
        if kind not in VALID_KINDS:
            return {"error": f"unknown kind: {kind}"}

        private_key = str(entry.get("private_key", "")).strip()
        public_key = str(entry.get("public_key", "")).strip()
        if kind == "ssh-key" and not private_key:
            return {"error": "an SSH-key credential needs a private key path"}

        secret = entry.get("secret")
        has_new_secret = isinstance(secret, str) and secret != ""
        if has_new_secret and not cred.available():
            return {"error": "credential storage unavailable on this system"}

        self._reload_credentials()
        cid = str(entry.get("id", "")).strip() or uuid.uuid4().hex[:12]

        meta: dict[str, Any] = {
            "id": cid,
            "label": label,
            "kind": kind,
            "username": str(entry.get("username", "")).strip(),
            "username_arg": str(entry.get("username_arg", "")).strip(),
            "secret_arg": str(entry.get("secret_arg", "")).strip(),
            "private_key": private_key,
            "public_key": public_key,
            "scope_hosts": _clean_str_list(entry.get("scope_hosts")),
            "scope_runnables": _clean_str_list(entry.get("scope_runnables")),
        }
        others = [c for c in self._credentials if str(c.get("id")) != cid]
        save_credentials(config_mod.credentials_path(), [*others, meta])
        if has_new_secret:
            try:
                cred.set_secret(cid, str(secret))  # has_new_secret ⇒ non-empty str
            except Exception as exc:
                return {"error": f"could not store secret: {exc}"}
        self._reload_credentials()
        return {"id": cid}

    def delete_credential(self, cred_id: str) -> None:
        """Remove a credential's metadata and its stored secret."""
        from . import credentials as cred
        from .credentials import save_credentials

        cid = str(cred_id or "")
        self._reload_credentials()
        remaining = [c for c in self._credentials if str(c.get("id")) != cid]
        save_credentials(config_mod.credentials_path(), remaining)
        cred.delete_secret(cid)
        self._reload_credentials()

    # ── config sync (pull config files from a git repo, by tag) ─────────────────

    def get_sync_config(self) -> dict[str, Any]:
        """The ``[config_sync]`` settings for the Settings → Config Sync tab.

        Never includes the secret (that lives in the keyring under
        ``credential_id``). Adds ``engine`` (the active git engine, or None when
        the ``git-sync`` extra isn't installed), ``files_default`` (the file set
        the UI offers), and ``preserve_default`` (the built-in keep-local paths
        always applied on top of the user's list) so the tab can render in one
        round-trip."""
        from . import configmerge, gitsync

        sync = read_sync()
        return {
            "url": sync.get("url", ""),
            "ref": sync.get("ref", ""),
            "credential_id": sync.get("credential_id", ""),
            "subpath": sync.get("subpath", ""),
            "layers": list(sync.get("layers") or []),
            "ca_bundle": sync.get("ca_bundle", ""),
            "tls_verify": sync.get("tls_verify", True) is not False,
            "files": list(sync.get("files") or gitsync.KNOWN_FILES),
            "preserve": list(sync.get("preserve") or []),
            "backups_keep": backups_keep_value(sync),
            "last_ref": sync.get("last_ref", ""),
            "last_commit": sync.get("last_commit", ""),
            "last_synced": sync.get("last_synced", ""),
            "engine": gitsync.engine_name(),
            "files_default": list(gitsync.KNOWN_FILES),
            "preserve_default": list(configmerge.DEFAULT_PRESERVE),
        }

    def save_sync_config(self, data: dict[str, Any]) -> None:
        """Persist the Config Sync settings, preserving the ``last_*`` stamps."""
        sync = read_sync()
        sync.update(
            {
                "url": str(data.get("url", "")).strip(),
                "ref": str(data.get("ref", "")).strip(),
                "credential_id": str(data.get("credential_id", "")).strip(),
                "subpath": str(data.get("subpath", "")).strip(),
                "layers": [
                    str(s).strip() for s in (data.get("layers") or []) if str(s).strip()
                ],
                "ca_bundle": str(data.get("ca_bundle", "")).strip(),
                "tls_verify": data.get("tls_verify", True) is not False,
                "files": [
                    str(f).strip() for f in (data.get("files") or []) if str(f).strip()
                ],
                "preserve": [
                    str(p).strip()
                    for p in (data.get("preserve") or [])
                    if str(p).strip()
                ],
                "backups_keep": backups_keep_value(data),
            }
        )
        write_sync(sync)

    def list_sync_tags(self) -> dict[str, Any]:
        """Tags advertised by the configured repo, for the ref dropdown. Best
        effort: returns ``{"error": ...}`` (the UI falls back to a free-text ref)
        rather than raising."""
        from . import gitsync

        sync = read_sync()
        url = str(sync.get("url", "")).strip()
        if not url:
            return {"error": "set a repository URL first"}
        try:
            auth = self._sync_auth(url, str(sync.get("credential_id", "")))
            return {"tags": gitsync.list_tags(url, auth)}
        except gitsync.GitSyncError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not list tags: {exc}"}

    def _sync_auth(
        self, url: str, credential_id: str, tls: dict[str, Any] | None = None
    ) -> Any:
        """Build a ``GitAuth`` for ``url`` from a saved credential.

        HTTPS uses the credential's username + keyring secret (token), the
        ``[config_sync] ca_bundle`` for cert verification (internal CA / TLS proxy),
        and the console's ``[ssh] proxy`` for egress. SSH prefers an ``ssh-key``
        credential's own ``private_key`` path (with its keyring passphrase, if
        any) so a repo can use a dedicated deploy key; it falls back to the
        console's configured SSH key (``[ssh] identityFile``) when the credential
        carries no key. The username defaults to ``git``. ``tls`` supplies
        ``ca_bundle`` / ``tls_verify`` from another section (Memory Sync) instead
        of ``[config_sync]``."""
        from . import credentials as cred
        from . import gitsync

        entry = self._credential_entry(credential_id) if credential_id else None
        username = str((entry or {}).get("username", "")).strip()
        cfg_ssh = self.get_config().get("ssh") or {}
        if gitsync._is_ssh_url(url):
            cred_key = str((entry or {}).get("private_key", "") or "").strip()
            key_path = cred_key or str(cfg_ssh.get("identityFile", "") or "")
            # An ssh-key credential's keyring secret is the key's passphrase.
            passphrase = ""
            if credential_id and str((entry or {}).get("kind", "")) == "ssh-key":
                passphrase = cred.get_secret(credential_id) or ""
            return gitsync.GitAuth(
                scheme="ssh",
                username=username or "git",
                key_path=key_path,
                passphrase=passphrase,
            )
        secret = cred.get_secret(credential_id) if credential_id else None
        sync = tls if tls is not None else read_sync()
        return gitsync.GitAuth(
            scheme="https",
            username=username,
            secret=secret or "",
            ca_bundle=str(sync.get("ca_bundle", "") or ""),
            proxy=str(cfg_ssh.get("proxy", "") or ""),
            tls_verify=sync.get("tls_verify", True) is not False,
        )

    def sync_pull(self, ref: str = "", expect_commit: str = "") -> dict[str, Any]:
        """Pull the configured files from the repo at the configured ref.

        ``ref`` overrides the configured ref for this pull (and becomes the
        configured ref when the pull succeeds) — so accepting a newer tag from
        the update indicator / preview is one step. ``expect_commit`` is the
        commit a :meth:`sync_preview` showed: when the ref now resolves to a
        different commit (the tag was moved, the branch advanced) the pull
        refuses and writes nothing, so what lands is exactly what was reviewed.

        When ``[config_sync] layers`` is set, each file is read at every layer
        subdir and **folded** low→high (a shared ``_base`` then a team dir, say)
        into one effective table before merging — so teams share a base and
        overlay their own flavour. With no layers it reads the single ``subpath``.

        Validates every fetched ``.toml`` before touching disk (a broken pull
        writes nothing) and backs up each existing file it actually changes (a
        no-op re-pull leaves no backup), keeping only the most recent
        ``backups_keep`` (default 5; 0 = keep all) per file and pruning the rest.
        The merge is
        **non-destructive**: collection files (hosts / triggers / filters /
        credentials / …) merge by each entry's identity (name / id) — adding new
        entries, updating matching ones, and keeping the operator's own; mapping
        files (``config.toml``) deep-overlay the repo on top of local. A
        ``preserve`` list (built-in defaults + the user's, e.g.
        ``config.toml:mailbox.folder`` / ``runspec_hosts.toml:*.room_user``) keeps
        machine-local keys local. The local ``[config_sync]`` is re-injected into
        ``config.toml`` and stamped with ``last_*``. Finally the running app
        hot-reloads hosts / filters / credentials / SSH / triggers, so no restart
        is needed.

        Returns ``{ok, ref, commit, written, skipped, backups, pruned}`` or
        ``{ok: False, error}``."""
        fetched = self._sync_fetch(ref)
        if not fetched.get("ok"):
            return fetched
        sync = fetched["sync"]
        ref = fetched["ref"]
        commit = fetched["commit"]
        if expect_commit and expect_commit != commit:
            return {
                "ok": False,
                "error": (
                    f"{ref} now points at {commit[:8]}, not the {expect_commit[:8]} "
                    "you previewed — nothing was written. Preview it again."
                ),
            }

        def _stamp() -> None:
            # Re-inject the local [config_sync] into the freshly-written config.toml
            # (so the repo pointer survives the overwrite) and stamp last_*. Runs
            # after the merge, before the hot-reload, so _after_config_change sees
            # the final config.
            sync["ref"] = ref
            sync["last_ref"] = ref
            sync["last_commit"] = commit
            sync["last_synced"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            write_sync(sync)

        applied = self._apply_config_layers(
            fetched["files"],
            fetched["layers"],
            preserve=fetched["preserve"],
            backups_keep=backups_keep_value(sync),
            post_merge=_stamp,
        )
        if not applied.get("ok"):
            return applied
        self._dispatch("runspec:config_sync_changed", {"ref": ref, "commit": commit})
        return {
            "ok": True,
            "ref": ref,
            "commit": commit,
            "written": applied["written"],
            "skipped": applied["skipped"],
            "backups": applied["backups"],
            "pruned": applied["pruned"],
        }

    def sync_preview(self, ref: str = "") -> dict[str, Any]:
        """What a pull at ``ref`` (default: the configured ref) would change.

        Fetches exactly as :meth:`sync_pull` does and computes each file's
        post-pull table with the same merge — but writes nothing. Per file:
        ``status`` (``changed`` / ``unchanged`` / ``new`` — no local file yet —
        / ``missing`` — not in the repo at that ref, so the pull skips it /
        ``invalid`` — not valid TOML, which would abort the pull), a structured
        ``summary`` (entries added/updated/removed, or changed keys) and a
        unified ``diff`` of the local file → the pulled result, both rendered as
        normalised TOML (so formatting-only differences don't show).

        Returns ``{ok, ref, commit, current_ref, current_commit, files, changed}``
        — pass ``commit`` back to :meth:`sync_pull` as ``expect_commit`` — or
        ``{ok: False, error}``."""
        from . import configmerge

        fetched = self._sync_fetch(ref)
        if not fetched.get("ok"):
            return fetched
        sync = fetched["sync"]
        ref_name = fetched["ref"]
        layers: dict[str, list[bytes]] = fetched["layers"]
        preserve = fetched["preserve"]
        files: list[dict[str, Any]] = []
        for name in fetched["files"]:
            datas = layers.get(name)
            if not datas:
                files.append({"name": name, "status": "missing"})
                continue
            try:
                parsed = [tomllib.loads(d.decode("utf-8")) for d in datas]
            except Exception as exc:  # noqa: BLE001
                files.append({"name": name, "status": "invalid", "error": str(exc)})
                continue
            incoming = configmerge.fold_layers(name, parsed)
            exists = (config_mod.app_dir() / name).exists()
            before = self._local_merge_table(name) if exists else {}
            after = configmerge.merged_native(
                name, before, incoming, preserve.get(name, [])
            )
            if name == "config.toml":
                # A pull re-injects the local [config_sync] after the merge, so
                # the repo's copy (if any) never lands — don't show it. Assign in
                # place (the pull stamps it where it sits), so its position in
                # the file doesn't read as a move on every preview.
                if "config_sync" in before:
                    after["config_sync"] = before["config_sync"]
                else:
                    after.pop("config_sync", None)
            summary = configmerge.change_summary(name, before, after)
            diff = configmerge.render_diff(
                name, before, after, from_label="local", to_label=ref_name
            )
            if not exists:
                status = "new"
            elif summary["changed"] or diff:
                status = "changed"
            else:
                status = "unchanged"
            files.append(
                {"name": name, "status": status, "summary": summary, "diff": diff}
            )
        return {
            "ok": True,
            "ref": ref_name,
            "commit": fetched["commit"],
            "current_ref": str(sync.get("last_ref", "") or ""),
            "current_commit": str(sync.get("last_commit", "") or ""),
            "changed": any(f["status"] in ("changed", "new", "invalid") for f in files),
            "files": files,
        }

    def sync_check_update(self) -> dict[str, Any]:
        """Is there newer config in the repo than the configured ref?

        One ``ls-remote`` (no objects fetched). A version tag (``v1.4.0``,
        ``emea-v2.1``) compares against the newest **release** tag in the same
        series (same prefix; pre-releases never offered); a branch reports
        whether its head moved past the last pulled commit. Feeds the footer
        indicator and the Config Sync tab.

        Returns ``{ok, configured, available, kind, current, latest}`` —
        ``configured: False`` (nothing to check) when no repo/ref is set or the
        git engine is missing — or ``{ok: False, error}``."""
        from . import gitsync

        sync = read_sync()
        url = str(sync.get("url", "")).strip()
        ref = str(sync.get("ref", "")).strip()
        if not url or not ref or not gitsync.available():
            return {"ok": True, "configured": False, "available": False}
        try:
            auth = self._sync_auth(url, str(sync.get("credential_id", "")))
            res = gitsync.check_update(
                url, ref, str(sync.get("last_commit", "") or ""), auth
            )
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"update check failed: {exc}"}
        return {
            "ok": True,
            "configured": True,
            "available": res.available,
            "kind": res.kind,
            "current": res.current,
            "latest": res.latest,
        }

    def _sync_fetch(self, ref: str = "") -> dict[str, Any]:
        """Fetch the configured files for a pull or its preview (no writes).

        ``ref`` overrides the configured ref. Returns ``{ok: True, sync, ref,
        commit, files, layers, preserve}`` — ``files`` the allowlisted names in
        order, ``layers`` each found file's raw bytes per layer (low→high),
        ``preserve`` parsed — or ``{ok: False, error}``."""
        from . import configmerge, gitsync

        sync = read_sync()
        url = str(sync.get("url", "")).strip()
        ref = str(ref or sync.get("ref", "")).strip()
        if not url or not ref:
            return {"ok": False, "error": "set a repository URL and a ref/tag first"}

        requested = [
            str(f).strip()
            for f in (sync.get("files") or gitsync.KNOWN_FILES)
            if str(f).strip()
        ]
        # Filename allowlist: bare names only — each is written into the app dir,
        # so a name with a path separator (or "..") could escape it.
        safe = [
            f
            for f in requested
            if "/" not in f and "\\" not in f and f not in ("", "..")
        ]
        # Overlay layers (low→high precedence); fall back to the single subpath.
        layers = [str(s).strip() for s in (sync.get("layers") or []) if str(s).strip()]
        if not layers:
            layers = [str(sync.get("subpath", "")).strip()]
        cred_id = str(sync.get("credential_id", "")).strip()

        try:
            auth = self._sync_auth(url, cred_id)
            result = gitsync.fetch_layers(url, ref, safe, auth, layers)
        except gitsync.GitSyncError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"pull failed: {exc}"}

        # Per file, the layers (in order) that carry it — the fold input.
        fetched_layers: dict[str, list[bytes]] = {}
        for name in safe:
            for layer in layers:
                data = result.layers.get(layer, {}).get(name)
                if data is not None:
                    fetched_layers.setdefault(name, []).append(data)
        if not fetched_layers:
            return {
                "ok": False,
                "error": f"none of the configured files were found at {ref} in the repo",
            }

        preserve = configmerge.parse_preserve(
            [*configmerge.DEFAULT_PRESERVE, *(sync.get("preserve") or [])]
        )
        return {
            "ok": True,
            "sync": sync,
            "ref": ref,
            "commit": result.commit,
            "files": safe,
            "layers": fetched_layers,
            "preserve": preserve,
        }

    def _apply_config_layers(
        self,
        requested: list[str],
        fetched_layers: dict[str, list[bytes]],
        *,
        preserve: dict[str, list[list[str]]],
        backups_keep: int,
        post_merge: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Validate, merge, and hot-reload a set of already-fetched config files.

        The **source-agnostic** half of a config update, shared by the git
        ``sync_pull`` and (S2+) the package ``seed_apply``. ``fetched_layers``
        maps each file name to its parsed layers' raw bytes (low→high);
        ``requested`` is the ordered name list, so a name with no bytes is
        reported as ``skipped``.

        Validates every ``.toml`` before touching disk (a broken file writes
        nothing). Per file: snapshots the prior file, folds its layers, merges
        the effective table into local config non-destructively (collections by
        identity, mappings by deep-overlay, ``preserve`` pinning machine-local
        keys), keeps the backup only if the merge changed the file, and prunes
        to ``backups_keep``. ``post_merge`` — caller-specific stamping that must
        land before the reload — runs after the loop; then the running app
        hot-reloads (hosts / filters / credentials / SSH / triggers) so no
        restart is needed.

        Returns ``{ok: True, written, skipped, backups, pruned}`` or, on a
        validation failure, ``{ok: False, error}`` (nothing written)."""
        from . import configmerge

        # Validate-before-replace: any unparseable TOML in any layer aborts it all.
        for name, datas in fetched_layers.items():
            if not name.endswith(".toml"):
                continue
            for data in datas:
                try:
                    tomllib.loads(data.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "error": f"{name} is not valid TOML — nothing was written: {exc}",
                    }

        # Files the merge engine understands; anything else is written verbatim.
        mergeable = {
            "config.toml",
            "runspec_triggers.toml",
            *configmerge.COLLECTION_FILES,
        }

        old_cfg = self.get_config()
        app = config_mod.app_dir()
        ts = time.strftime("%Y%m%d-%H%M%S")
        written: list[str] = []
        skipped: list[str] = []
        backups: list[str] = []
        pruned: list[str] = []

        for name in requested:
            datas = fetched_layers.get(name)
            if not datas:
                skipped.append(name)
                continue
            dest = app / name
            # Snapshot the prior file before the merge overwrites it, then keep the
            # backup only if the apply actually changed the file (a re-apply of the
            # same content is a no-op and should leave no backup behind).
            backup: Path | None = None
            if dest.exists():
                backup = dest.with_name(f"{name}.bak-{ts}")
                try:
                    shutil.copy2(dest, backup)
                except Exception:
                    backup = None
                    logger.warning(
                        "could not back up %s before apply", name, exc_info=True
                    )
            if name in mergeable:
                parsed_layers = [tomllib.loads(d.decode("utf-8")) for d in datas]
                effective = configmerge.fold_layers(name, parsed_layers)
                self._merge_synced_file(name, effective, preserve.get(name, []))
            else:
                # An unrecognised file (a future addition) — top layer verbatim.
                self._atomic_write_bytes(dest, datas[-1])
            written.append(name)
            if backup is not None:
                unchanged = False
                try:
                    unchanged = filecmp.cmp(dest, backup, shallow=False)
                except Exception:
                    unchanged = False
                if unchanged:
                    backup.unlink(missing_ok=True)
                else:
                    backups.append(backup.name)
            pruned.extend(self._prune_backups(app, name, backups_keep))

        if post_merge is not None:
            post_merge()

        # Hot-reload so the running app picks up the new config without a restart.
        self._reload_hosts()
        self._reload_filters()
        self._reload_credentials()
        self._after_config_change(old_cfg, self.get_config())
        try:
            self.restart_triggers()
        except Exception:
            logger.debug("restart_triggers after config apply failed", exc_info=True)
        # Nudge the UI to re-read the merged runbooks (refreshes the `$` menu).
        if "runspec_runbooks.toml" in written:
            self._dispatch("runspec:runbooks_changed", {})

        return {
            "ok": True,
            "written": written,
            "skipped": skipped,
            "backups": backups,
            "pruned": pruned,
        }

    def seed_apply(self, force: bool = False) -> dict[str, Any]:
        """Apply the bundled package config seed and record the outcome.

        Thin wrapper over :meth:`_run_seed_apply` that stores the last error
        (``""`` on success) on ``self._seed_last_error``, so a failed apply is
        visible in :meth:`seed_status` (the Settings → Config Sync panel) and can
        be logged at startup, instead of being silently discarded. A single
        malformed bundled file aborts the whole apply, which used to leave a
        fresh install unconfigured with no trace of why."""
        result = self._run_seed_apply(force)
        self._seed_last_error = (
            "" if result.get("ok", True) else str(result.get("error") or "")
        )
        return result

    def _run_seed_apply(self, force: bool = False) -> dict[str, Any]:
        """Apply the baseline config bundled in the deployment package.

        Discovers seed config contributed by installed distributions
        (``runspec_console.config_seed`` entry points) and — when its
        ``{dist: version}`` stamp differs from the machine-local record, or
        ``force`` — folds it onto local config through the **same**
        non-destructive merge a git pull uses (``_apply_config_layers``).
        **Re-assert-on-upgrade:** a package upgrade rolls the baseline forward;
        between upgrades the stamp is unchanged and this is a pure no-op (nothing
        written, no backups). The seed is the **lowest** config layer — a later
        git pull and the operator's own edits top up over it, with ``preserve``
        and local-only entries winning. A seed may carry ``[config_sync]`` to
        bootstrap the live layer, but the machine-local ``last_*`` run-state is
        preserved (see ``_SEED_PRESERVE``).

        Secrets are never carried in the package (same contract as Config Sync) —
        the seed delivers metadata and structure only.

        Returns ``{ok: True, applied: bool, ...}`` or ``{ok: False, error}``;
        ``applied`` is False (with a ``reason``) when nothing is bundled or the
        stamp is unchanged."""
        from . import configmerge, configseed, gitsync

        try:
            fetched_layers, stamp = configseed.discover()
        except Exception as exc:  # noqa: BLE001 - discovery must never be fatal
            return {"ok": False, "error": f"seed discovery failed: {exc}"}
        if not fetched_layers:
            return {"ok": True, "applied": False, "reason": "no seed bundled"}

        stored = config_mod.read_seed_state().get("applied", {})
        if not force and stamp == stored:
            return {"ok": True, "applied": False, "reason": "unchanged", "stamp": stamp}

        # KNOWN_FILES order for a stable per-file apply sequence.
        requested = [n for n in gitsync.KNOWN_FILES if n in fetched_layers]
        # A seed may itself declare what operators own via [config_sync].preserve.
        # Honour it alongside the local list so a seed-declared preserve protects
        # from the *first* re-assert — otherwise it's a machine the operator has
        # to pre-configure locally, and the very upgrade that ships the preserve
        # runs before it takes effect (reads local config first).
        preserve = configmerge.parse_preserve(
            [
                *configmerge.DEFAULT_PRESERVE,
                *_SEED_PRESERVE,
                *self._seed_declared_preserve(fetched_layers),
                *(self.get_config().get("config_sync", {}).get("preserve") or []),
            ]
        )

        def _stamp() -> None:
            # Record what we just applied so the next boot is a no-op until the
            # package version changes again.
            config_mod.write_seed_state(
                {
                    "applied": stamp,
                    "last_applied": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )

        applied = self._apply_config_layers(
            requested,
            fetched_layers,
            preserve=preserve,
            backups_keep=backups_keep_value(read_sync()),
            post_merge=_stamp,
        )
        if not applied.get("ok"):
            return applied
        return {
            "ok": True,
            "applied": True,
            "stamp": stamp,
            "written": applied["written"],
            "skipped": applied["skipped"],
            "backups": applied["backups"],
            "pruned": applied["pruned"],
        }

    def seed_status(self) -> dict[str, Any]:
        """Read-only status of the package config seed, for the Settings UI.

        Returns the discovered ``{dist: version}`` stamp (what the installed
        package bundles now), the machine-local applied stamp + timestamp (what
        was last applied here), whether any seed is bundled at all, whether
        the bundled version differs from the applied one (a re-assert is
        ``pending`` — e.g. just after a package upgrade, before the next
        launch), and **``overrides``** — the seeded keys whose current *local*
        value differs from the seed's, so an operator can see what a re-assert
        (or the next upgrade) would silently revert. Never raises."""
        from . import configseed

        try:
            _fetched, stamp = configseed.discover()
        except Exception:  # noqa: BLE001 - status must never be fatal
            _fetched, stamp = {}, {}
        state = config_mod.read_seed_state()
        applied = state.get("applied", {})
        if not isinstance(applied, dict):
            applied = {}
        return {
            "available": bool(stamp),
            "stamp": stamp,
            "applied": applied,
            "last_applied": str(state.get("last_applied", "")),
            "pending": bool(stamp) and stamp != applied,
            "overrides": self._seed_overrides(_fetched),
            # The last apply error (from startup or a UI re-apply), so a malformed
            # bundled file surfaces in the panel instead of vanishing.
            "error": getattr(self, "_seed_last_error", ""),
        }

    def _seed_overrides(self, fetched: dict[str, list[bytes]]) -> list[dict[str, Any]]:
        """The seeded leaf keys whose current local value differs from the seed's.

        For each bundled file, folds the seed's layers to its effective table and
        diffs it against the on-disk local file, honouring the same ``preserve``
        list ``seed_apply`` uses (so a protected key is flagged, not hidden). The
        custom-merged triggers file is skipped (its diff would be misleading).
        Best-effort — any failure yields ``[]`` rather than breaking the status."""
        from . import configmerge, gitsync

        if not fetched:
            return []
        try:
            preserve = configmerge.parse_preserve(
                [
                    *configmerge.DEFAULT_PRESERVE,
                    *_SEED_PRESERVE,
                    *(self.get_config().get("config_sync", {}).get("preserve") or []),
                ]
            )
            rows: list[dict[str, Any]] = []
            for name in gitsync.KNOWN_FILES:
                if name not in fetched or name == "runspec_triggers.toml":
                    continue
                parsed_layers = [
                    tomllib.loads(d.decode("utf-8")) for d in fetched[name]
                ]
                effective = configmerge.fold_layers(name, parsed_layers)
                local = self._local_native_table(name)
                rows.extend(
                    configmerge.seed_overrides(
                        name, effective, local, preserve.get(name, [])
                    )
                )
            return rows
        except Exception:  # noqa: BLE001 - status must never be fatal
            logger.debug("seed override computation failed", exc_info=True)
            return []

    def _seed_declared_preserve(
        self, fetched_layers: dict[str, list[bytes]]
    ) -> list[str]:
        """The ``[config_sync].preserve`` list the seed's own ``config.toml``
        declares (``[]`` when absent/unparseable).

        Read from the fetched bytes — not the on-disk file — so it's available
        before the seed is applied and protects the operator's local values from
        the very re-assert that ships it. Best-effort: a malformed seed yields
        ``[]`` here and is caught again by the validate-before-write gate."""
        from . import configmerge

        layers = fetched_layers.get("config.toml")
        if not layers:
            return []
        try:
            folded = configmerge.fold_layers(
                "config.toml",
                [tomllib.loads(d.decode("utf-8")) for d in layers],
            )
            entries = folded.get("config_sync", {}).get("preserve") or []
            return [str(e) for e in entries]
        except Exception:  # noqa: BLE001
            return []

    def _local_native_table(self, name: str) -> dict[str, Any]:
        """Parse the on-disk local config file ``name`` into its native top-level
        shape (``{}`` when absent/unreadable) — the operator's actual values, for
        comparing against the folded seed."""
        path = config_mod.app_dir() / name
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as fh:
                return tomllib.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    def _prune_backups(self, app: Path, name: str, keep: int) -> list[str]:
        """Delete all but the ``keep`` most recent ``<name>.bak-<ts>`` files.

        The ``%Y%m%d-%H%M%S`` timestamp is fixed-width, so a lexical sort of the
        names is chronological. ``keep <= 0`` disables pruning (keep all). Returns
        the names removed (best-effort — a failed unlink is logged, not raised)."""
        if keep <= 0:
            return []
        existing = sorted(app.glob(f"{name}.bak-*"))
        removed: list[str] = []
        for old in existing[:-keep]:
            try:
                old.unlink()
                removed.append(old.name)
            except Exception:
                logger.warning("could not prune old backup %s", old.name, exc_info=True)
        return removed

    def _atomic_write_bytes(self, dest: Path, data: bytes) -> None:
        """Write ``data`` to ``dest`` atomically (temp + os.replace), mirroring
        ``config.write_config``."""
        tmp = dest.with_name(f"{dest.name}.tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    def _merge_synced_file(
        self, name: str, parsed: dict[str, Any], preserve: list[list[str]]
    ) -> None:
        """Merge one pulled file's effective table into local config, non-destructively.

        ``parsed`` is the file's already-folded layers (a single overlay collapses
        to itself), in the file's native top-level shape. The post-pull table is
        :func:`configmerge.merged_native` of the local table — the same function
        the preview (:meth:`sync_preview`) diffs, so what the operator reviewed is
        what lands. Collection files merge by identity (``name``/``id``); mapping
        files deep-overlay; ``preserve`` paths keep machine-local keys local."""
        from . import configmerge

        local = self._local_merge_table(name)
        after = configmerge.merged_native(name, local, parsed, preserve)
        self._write_merged_table(name, after)

    def _local_merge_table(self, name: str) -> dict[str, Any]:
        """The local side of a pull merge for ``name``, native-shaped.

        Read through each store's own loader (which migrates legacy files and
        quarantines a corrupt one) rather than raw TOML, so the merge — and its
        preview — start from what the app actually uses."""
        from . import configmerge

        if name == "config.toml":
            return dict(self.get_config())
        if name == "runspec_hosts.toml":
            return {"host": hosts_mod.load_hosts(config_mod.hosts_path())}
        if name == "runspec_groups.toml":
            from .groups import load_groups

            return {"group": load_groups(config_mod.groups_path())}
        if name == "runspec_profiles.toml":
            from .profiles import load_profiles

            return {"profile": load_profiles(config_mod.profiles_path())}
        if name == "runspec_filters.toml":
            from .filters_store import load_filters

            return {"filter": load_filters(filters_path())}
        if name == "runspec_credentials.toml":
            from .credentials import load_credentials

            return {"credential": load_credentials(config_mod.credentials_path())}
        if name == "runspec_runbooks.toml":
            from . import memory

            return {"runbook": memory.load_runbooks()}
        if name == "runspec_saved_runs.toml":
            from .savedruns import load_saved_runs

            return {"saved_run": load_saved_runs()}
        if name == "runspec_sounds.toml":
            from .sounds_registry import load_rows

            return {"sound": load_rows()}
        if name == "runspec_repos.toml":
            from .repos import load_repos

            return {"repo": load_repos(config_mod.repos_path())}
        if name == "runspec_self_service.toml":
            from .self_service import load_self_service

            return {"self_service": load_self_service(config_mod.self_service_path())}
        if name == configmerge.TRIGGERS_FILE:
            from .config import read_triggers

            triggers: dict[str, Any] = {}
            for trig in read_triggers():
                body = dict(trig)
                key = body.pop("name", None)
                if key:
                    triggers[str(key)] = body
            return {"triggers": triggers}
        return self._local_native_table(name)

    def _write_merged_table(self, name: str, table: dict[str, Any]) -> None:
        """Persist a merged native table for ``name`` through its store's saver.

        Every file in ``gitsync.KNOWN_FILES`` has a branch (each store normalises
        on save); an unrecognised collection falls back to a plain TOML write.

        The triggers file keeps the local rota as-is — working hours are
        per-person and never synced (a legacy repo ``[rota]`` is ignored)."""
        from . import configmerge

        def rows(key: str) -> list[dict[str, Any]]:
            raw = table.get(key)
            return list(raw) if isinstance(raw, list) else []

        if name == "config.toml":
            config_mod.write_config(table)
        elif name == "runspec_hosts.toml":
            hosts_mod.save_hosts(config_mod.hosts_path(), rows("host"))
        elif name == "runspec_groups.toml":
            from .groups import save_groups

            save_groups(config_mod.groups_path(), rows("group"))
        elif name == "runspec_profiles.toml":
            from .profiles import save_profiles

            save_profiles(config_mod.profiles_path(), rows("profile"))
        elif name == "runspec_filters.toml":
            from .filters_store import save_filters

            save_filters(filters_path(), rows("filter"))
        elif name == "runspec_credentials.toml":
            from .credentials import save_credentials

            save_credentials(config_mod.credentials_path(), rows("credential"))
        elif name == "runspec_runbooks.toml":
            from . import memory

            memory.save_runbooks(rows("runbook"))
        elif name == "runspec_saved_runs.toml":
            from .savedruns import save_saved_runs

            save_saved_runs(rows("saved_run"))
        elif name == "runspec_sounds.toml":
            from .sounds_registry import save_rows

            save_rows(rows("sound"))
        elif name == "runspec_repos.toml":
            from .repos import save_repos

            save_repos(config_mod.repos_path(), rows("repo"))
        elif name == "runspec_self_service.toml":
            from .self_service import save_self_service

            save_self_service(config_mod.self_service_path(), rows("self_service"))
        elif name == configmerge.TRIGGERS_FILE:
            from .config import read_rota, write_triggers_file

            raw = table.get("triggers")
            triggers = (
                [{**v, "name": k} for k, v in raw.items() if isinstance(v, dict)]
                if isinstance(raw, dict)
                else []
            )
            write_triggers_file(read_rota(), triggers)
        else:
            import tomli_w

            dest = config_mod.app_dir() / name
            self._atomic_write_bytes(dest, tomli_w.dumps(table).encode("utf-8"))

    def import_jump_hosts(self, toml_content: str) -> list[dict[str, Any]]:
        try:
            data = tomllib.loads(toml_content)
        except Exception:
            return []
        imported = data.get("host", [])
        existing = {h["name"] for h in self._hosts}
        new = [h for h in imported if h.get("name") and h["name"] not in existing]
        self._hosts.extend(new)
        hosts_mod.save_hosts(config_mod.hosts_path(), self._hosts)
        self._kick_refresh(force=True)
        return new

    # ── invocation ────────────────────────────────────────────────────────────
