"""bridge_hosts.py — HostsMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

from typing import Any


from . import bridge_utils as utils_mod
from . import hosts as hosts_mod


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class HostsMixin:
    def get_hosts(self) -> list[dict[str, Any]]:
        # The sidebar shows the active profile's working set only — out-of-profile
        # hosts are dormant (no standing connection). _active_host_entries honours
        # the machine-local active profile; All hosts (blank) returns the full set.
        all_hosts = self._active_host_entries()
        result: list[dict[str, Any]] = []
        for h in all_hosts:
            paths = utils_mod._paths(h)
            with self._lock:
                # Local is always connected; remotes use last cached probe result
                # (defaults to False until first probe completes)
                connected = self._connected_cache.get(h["name"], h.get("ssh") is None)
            result.append(
                {
                    "name": h["name"],
                    "connected": connected,
                    "runnableCount": 0,  # filled by get_runnables
                    "groups": [hosts_mod.venv_name(p) for p in paths],
                    "role": h.get("role"),
                    "group": h.get("group"),
                    "profiles": hosts_mod.host_profiles(h),
                }
            )
        return result

    def manage_denylist(self, payload: dict[str, Any]) -> str:
        """Public wrapper for the denylist kill switch (the hosts-panel Denylist
        modal).

        Runs ``runspec deny <action>`` across the selected fleet hosts (one call
        per venv) and returns the aggregated per-host/per-venv summary. Same
        handler the agent's ``manage_denylist`` tool uses. ``payload`` keys:
        ``action`` (list|add|all|clear|mode), ``targets`` (host names; empty ⇒
        all), ``runnable``/``user``/``host`` (globs, for add), ``reason``,
        ``setting`` (for mode). It edits remote denylists via the core
        ``runspec`` binary (exempt from its own denylist), so ``clear`` always
        re-enables and the console can never be locked out.
        """
        return self._run_manage_denylist(dict(payload or {}))

    def denylist_status(self, targets: list[str] | None = None) -> list[dict[str, Any]]:
        """Structured denylist state per host/venv (the hosts-panel Denylist modal).

        Runs ``runspec deny list --json`` on each selected host (one call per
        venv) and parses it. ``targets`` selects host names (empty/None ⇒ all
        hosts with a runspec path). Each row: ``{host, venv, ok, error?, path,
        present, mode, disabled, reason, rules}``. Read-only — no prompt.
        """
        import json as _json
        import shlex
        import subprocess
        from concurrent.futures import ThreadPoolExecutor

        from .procutil import no_window_kwargs

        wanted = {str(t) for t in (targets or [])}
        # Deliberately the full configured fleet (``self._hosts``), not the active
        # profile's working set — the denylist is a fleet kill switch; the modal
        # passes the currently *connected* hosts as ``targets`` so a down host
        # never waits out an SSH timeout here.
        hosts = [h for h in self._hosts if utils_mod._paths(h)]
        if wanted:
            hosts = [h for h in hosts if h.get("name") in wanted]

        tasks = [(h, rp) for h in hosts for rp in utils_mod._paths(h)]

        def _one(task: tuple[dict[str, Any], str]) -> dict[str, Any]:
            h, rp = task
            host_name = str(h.get("name"))
            ssh = h.get("ssh")
            idf = h.get("identityFile")
            venv = hosts_mod.venv_name(rp)
            argv = [rp, "deny", "list", "--json"]
            row: dict[str, Any] = {"host": host_name, "venv": venv, "ok": False}
            try:
                if ssh:
                    cmd = " ".join(shlex.quote(p) for p in argv)
                    code, out, err = self._ssh_run(
                        ssh, cmd, identity_file=idf, timeout=20
                    )
                else:
                    proc = subprocess.run(
                        argv,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        **no_window_kwargs(),
                    )
                    code, out, err = proc.returncode, proc.stdout, proc.stderr
                if code != 0:
                    row["error"] = (err or out or f"exit {code}").strip()
                else:
                    row.update(_json.loads(out))
                    row["ok"] = True
            except Exception as exc:  # noqa: BLE001 — surface any failure as a row
                row["error"] = str(exc)
            return row

        if not tasks:
            return []
        # One SSH exec per venv, fanned out across hosts in parallel (bounded by
        # [refresh] workers; handshakes by the pool's max_concurrent) instead of
        # serially — a 70-host fleet used to take minutes here. ``ex.map`` keeps
        # submission order, so rows come back host-by-host, venv-by-venv.
        with ThreadPoolExecutor(
            max_workers=min(self._fanout_workers(), len(tasks)),
            thread_name_prefix="denylist",
        ) as ex:
            return list(ex.map(_one, tasks))

    def get_runnables(self, host: str) -> list[dict[str, Any]]:
        with self._lock:
            cache = list(self._runnables_cache)
        if host == "all":
            return cache
        return [r for r in cache if r.get("host") == host]

    def discovery_state(self) -> dict[str, Any]:
        """Fleet-discovery readiness for the UI: ``{ready, done, total}``.

        ``ready`` flips true once the first full refresh cycle has finished; until
        then ``done``/``total`` track how many hosts have been discovered so the UI
        can show a "discovering N/M hosts…" indicator. Read on mount (the
        ``runspec:discovery_state`` events may predate the view); the live updates
        come over that event. ``total`` is 0 before the first cycle starts."""
        with self._lock:
            done, total = self._discovery_progress
            return {"ready": self._discovery_ready, "done": done, "total": total}

    def list_dir(
        self,
        host: str,
        path: str = "",
        group: str | None = None,
        dirs_only: bool = False,
        runnable: str | None = None,
        as_service_account: bool = False,
    ) -> dict[str, Any]:
        """List a directory on ``host`` for the Forms path-picker.

        ``path`` empty means the connecting user's home directory. Returns
        ``{cwd, parent, entries, truncated}`` (see ``fs_browse``) or
        ``{"error": ...}``. By default lists as the SSH login user (or the console
        user locally). With ``as_service_account`` and a ``runnable``, the remote
        listing is wrapped in that runnable's ``run_as``/become — so the picker
        shows what the job itself would see. ``group`` disambiguates a runnable
        that exists in more than one venv on the host.
        """
        entry = self._host_entry(host)
        if entry is None:
            return {"error": f"unknown host {host}"}
        try:
            if entry.get("ssh"):
                from .fs_browse import parse_remote_listing, remote_list_command

                run_as, become_method, become_flags = "", "sudo", None
                if as_service_account:
                    if not runnable:
                        return {
                            "error": "service-account view needs a runnable context"
                        }
                    with self._lock:
                        cached = next(
                            (
                                r
                                for r in self._runnables_cache
                                if r.get("host") == host
                                and r.get("name") == runnable
                                and (group is None or r.get("group") == group)
                            ),
                            None,
                        )
                    run_as, become_method, become_flags = self._become_params(
                        cached, host
                    )
                    if not run_as:
                        return {
                            "error": f"no service account (run_as) configured for {runnable} on {host}"
                        }

                cmd = remote_list_command(
                    path,
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                )
                code, out, err = self._ssh_run(
                    entry["ssh"],
                    cmd,
                    identity_file=entry.get("identityFile"),
                    timeout=15,
                )
                if code != 0:
                    who = f" as {run_as}" if run_as else ""
                    return {
                        "error": err.strip()
                        or f"could not list {path or '~'}{who} (exit {code})"
                    }
                return parse_remote_listing(out, dirs_only=dirs_only)

            from .fs_browse import list_local

            return list_local(path, dirs_only=dirs_only)
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    # ── file transfer (SFTP) ────────────────────────────────────────────────────
