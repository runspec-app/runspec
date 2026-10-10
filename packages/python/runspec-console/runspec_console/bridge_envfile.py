"""bridge_envfile.py — EnvFileMixin: manage each venv's ``.runspec_env``.

The hosts-panel **Env files** window edits the dotenv file core loads into every
run (``{venv}/.runspec_env``), for the local venvs and the venvs of the
connected remote hosts:

* one venv at a time — a key/value table showing which runnables read each
  variable, with secret-looking values masked;
* one key across many venvs — set or remove it everywhere you pick (by host,
  group or profile), previewed first, touching no other key.

Pure parsing / patching lives in :mod:`envfile`; the file itself is read and
written by :mod:`_envfile_worker` — in-process for a local venv, and on a remote
host by the venv's own ``bin/python`` over the pooled SSH connection. When the
SSH login can't read or write the file, the worker is re-run as the venv's
owner (``sudo -n -u <owner>``), then as root, the way the denylist escalates;
a scoped NOPASSWD rule on the venv's ``bin/`` covers it. A write's text rides
stdin, never the command line, and lands as a temp file renamed over the old
one only if the file still matches what was read (no lost updates).

Secrets: a masked value is never sent to the frontend unless the operator
reveals it, and the audit records (console + the venv's own ledger via
``runspec audit record``) carry key names only, never values.
"""

from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _envfile_worker as worker
from . import bridge_utils as utils_mod
from . import envfile as ef
from . import hosts as hosts_mod
from . import marketplace as mp
from . import provision as prov
from .bridge_const import logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401

_SSH_TIMEOUT_S = 30
_WORKER_SRC: str | None = None


def _worker_source() -> str:
    global _WORKER_SRC
    if _WORKER_SRC is None:
        _WORKER_SRC = Path(worker.__file__).read_text(encoding="utf-8")
    return _WORKER_SRC


def _parse_worker_output(out: str) -> dict[str, Any] | None:
    for line in reversed((out or "").splitlines()):
        if line.startswith(worker.MARKER):
            try:
                res = json.loads(line[len(worker.MARKER) :])
            except ValueError:
                return None
            return res if isinstance(res, dict) else None
    return None


class EnvFileMixin:
    _connected_cache: dict[str, bool]
    _lock: threading.Lock
    _runnables_cache: list[dict[str, Any]]

    # ── JS API ──

    def env_file_targets(self, hosts: list[str] | None = None) -> list[dict[str, Any]]:
        """Every venv whose ``.runspec_env`` can be managed: the local venvs plus
        each runspec path of the named remote hosts (the UI passes the connected
        hosts of the active profile). One row per venv:
        ``{host, runspec_path, venv, root, path, group, profiles, local,
        connected, supported, reason}``."""
        wanted = {str(n) for n in (hosts or [])}
        with self._lock:
            connected = dict(self._connected_cache)
        rows: list[dict[str, Any]] = []
        all_fn = getattr(self, "_all_host_entries", None)
        entries = list(all_fn()) if callable(all_fn) else []
        for h in entries:
            name = str(h.get("name") or "")
            is_local = not h.get("ssh")
            if not is_local and wanted and name not in wanted:
                continue
            for rp in utils_mod._paths(h):
                row: dict[str, Any] = {
                    "host": name,
                    "runspec_path": rp,
                    "venv": hosts_mod.venv_name(rp),
                    "group": str(h.get("group") or ""),
                    "profiles": hosts_mod.host_profiles(h),
                    "local": is_local,
                    "connected": True if is_local else bool(connected.get(name)),
                    "supported": True,
                    "reason": "",
                }
                try:
                    root = ef.local_root(rp) if is_local else ef.remote_root(rp)
                    row["root"] = root
                    sep = "\\" if ef.is_windows_path(root) else "/"
                    row["path"] = root.rstrip("/\\") + sep + ef.FILE_NAME
                except ef.EnvFileError as exc:
                    row.update(supported=False, reason=str(exc), root="", path="")
                rows.append(row)
        return rows

    def env_file_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One venv's file as a table (``payload``: ``{host, runspec_path}``):
        entries with their readers (secret values masked), suggested variables
        the venv's runnables read but the file doesn't set, the file's owner and
        mode, whether it can be saved (and as which account), and warnings."""
        t = self._env_target(payload)
        res, as_user = self._env_access(t, need_write=True)
        return self._env_view(t, res, as_user)

    def env_file_reveal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The value of one (masked) key, read fresh: ``{key, value, found}``.
        Recorded in the console audit log (the key name, not the value)."""
        t = self._env_target(payload)
        key = ef.validate_key(str((payload or {}).get("key") or ""))
        res, _as = self._env_access(t, need_write=False)
        if not res.get("ok") or not res.get("readable") or res.get("error"):
            raise ValueError(res.get("error") or f"Can't read {t['path']}.")
        values = ef.parse(res.get("text") or "").values()
        self._env_audit("venv.env.revealed", t, keys=[key])
        return {"key": key, "value": values.get(key, ""), "found": key in values}

    def env_file_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply ``{set: {KEY: value}, unset: [KEY]}`` to one venv's file.
        ``sha`` is the hash the editor loaded; the save is refused (``conflict``)
        when the file changed since. Returns the fresh view plus ``changes``."""
        payload = dict(payload or {})
        t = self._env_target(payload)
        try:
            sets, drops = ef.normalize_changes(payload.get("set"), payload.get("unset"))
        except ef.EnvFileError as exc:
            raise ValueError(str(exc)) from exc
        out = self._env_patch_one(t, sets, drops, expect=str(payload.get("sha") or ""))
        if not out.get("ok"):
            return out
        res, as_user = self._env_access(t, need_write=True)
        view = self._env_view(t, res, as_user)
        view["changes"] = out.get("changes")
        view["saved"] = out.get("written", False)
        return view

    def env_file_bulk_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read-only: for one key change across ``targets`` (``[{host,
        runspec_path}]``), each venv's current state — ``absent`` / ``same`` /
        ``differs`` for a set, ``absent`` / ``present`` for a removal — and
        whether it can be written. Values are compared, never returned."""
        targets, key, value = self._env_bulk_args(payload)

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            row = self._env_row(t)
            try:
                res, as_user = self._env_access(t, need_write=True)
                if not res.get("ok") or not res.get("readable") or res.get("error"):
                    row["error"] = res.get("error") or "unreadable"
                    return row
                row.update(
                    ok=True,
                    state=ef.key_state(res.get("text") or "", key, value),
                    writable=bool(res.get("writable")),
                    as_user=as_user or str(res.get("user") or ""),
                    exists=bool(res.get("exists")),
                )
                if not res.get("writable"):
                    row["error"] = res.get("write_error") or "not writable"
            except Exception as exc:  # noqa: BLE001 — one venv never breaks the table
                row["error"] = str(exc)
            return row

        return {
            "key": key,
            "action": "set" if value is not None else "unset",
            "rows": self._env_fanout(_one, targets),
        }

    def env_file_bulk_apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set or remove one key in every venv of ``targets``; each file is read
        fresh, patched by key and written back only if unchanged in between. A
        venv already in the wanted state isn't rewritten."""
        targets, key, value = self._env_bulk_args(payload)
        sets = {key: value} if value is not None else {}
        drops = [] if value is not None else [key]

        def _one(t: dict[str, Any]) -> dict[str, Any]:
            row = self._env_row(t)
            try:
                out = self._env_patch_one(t, sets, drops, expect=None)
                row.update(
                    ok=bool(out.get("ok")),
                    changed=bool(out.get("written")),
                    as_user=out.get("as_user", ""),
                    error=out.get("error", ""),
                )
            except Exception as exc:  # noqa: BLE001
                row["error"] = str(exc)
            return row

        rows = self._env_fanout(_one, targets)
        return {
            "key": key,
            "action": "set" if value is not None else "unset",
            "rows": rows,
            "ok": sum(1 for r in rows if r.get("ok")),
            "changed": sum(1 for r in rows if r.get("changed")),
            "failed": sum(1 for r in rows if not r.get("ok")),
        }

    # ── internals ──

    def _env_target(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        p = dict(payload or {})
        host = str(p.get("host") or "")
        rp = str(p.get("runspec_path") or "")
        entry_fn = getattr(self, "_host_entry", None)
        h = entry_fn(host) if callable(entry_fn) else None
        if h is None:
            raise ValueError(f"{host or 'That host'} is not a configured host.")
        if rp not in utils_mod._paths(h):
            raise ValueError(f"{rp} is not one of {host}'s runspec paths.")
        local = not h.get("ssh")
        try:
            root = ef.local_root(rp) if local else ef.remote_root(rp)
        except ef.EnvFileError as exc:
            raise ValueError(str(exc)) from exc
        sep = "\\" if ef.is_windows_path(root) else "/"
        return {
            "host": host,
            "runspec_path": rp,
            "venv": hosts_mod.venv_name(rp),
            "root": root,
            "path": root.rstrip("/\\") + sep + ef.FILE_NAME,
            "local": local,
            "entry": h,
        }

    @staticmethod
    def _env_row(t: dict[str, Any]) -> dict[str, Any]:
        return {
            "host": t["host"],
            "runspec_path": t["runspec_path"],
            "venv": t["venv"],
            "path": t["path"],
            "ok": False,
        }

    def _env_bulk_args(
        self, payload: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        p = dict(payload or {})
        try:
            key = ef.validate_key(str(p.get("key") or ""))
            action = str(p.get("action") or "set")
            if action not in ("set", "unset"):
                raise ef.EnvFileError("Action must be set or unset.")
            value = ef.validate_value(key, p.get("value")) if action == "set" else None
        except ef.EnvFileError as exc:
            raise ValueError(str(exc)) from exc
        targets = []
        seen: set[tuple[str, str]] = set()
        for raw in p.get("targets") or []:
            if not isinstance(raw, dict):
                continue
            t = self._env_target(raw)
            k = (t["host"], t["runspec_path"])
            if k not in seen:
                seen.add(k)
                targets.append(t)
        if not targets:
            raise ValueError("Pick at least one venv.")
        return targets, key, value

    def _env_fanout(self, fn: Any, items: list[Any]) -> list[dict[str, Any]]:
        if not items:
            return []
        workers_fn = getattr(self, "_fanout_workers", None)
        try:
            workers = int(workers_fn()) if callable(workers_fn) else 4
        except Exception:
            workers = 4
        with ThreadPoolExecutor(
            max_workers=min(max(1, workers), len(items)), thread_name_prefix="envfile"
        ) as ex:
            return list(ex.map(fn, items))

    def _env_records(self, t: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            cache = list(getattr(self, "_runnables_cache", []) or [])
        return [
            r
            for r in cache
            if r.get("host") == t["host"] and r.get("group") == t["venv"]
        ]

    def _env_view(
        self, t: dict[str, Any], res: dict[str, Any], as_user: str
    ) -> dict[str, Any]:
        view: dict[str, Any] = self._env_row(t)
        view.update(
            root=t["root"],
            local=t["local"],
            exists=bool(res.get("exists")),
            owner=str(res.get("owner") or ""),
            user=str(res.get("user") or ""),
            as_user=as_user,
            mode=str(res.get("mode") or ""),
            sha=str(res.get("sha") or ""),
            writable=bool(res.get("writable")),
            write_error=str(res.get("write_error") or ""),
            warnings=[],
            entries=[],
            suggestions=[],
            overrides=[],
        )
        if not res.get("ok") or not res.get("readable") or res.get("error"):
            view["error"] = str(res.get("error") or f"Can't read {t['path']}.")
            view["writable"] = False
            return view
        view["ok"] = True
        records = self._env_records(t)
        readers = ef.readers(records)
        view.update(ef.entry_rows(res.get("text") or "", readers))
        view["overrides"] = ef.overrides(records)
        warnings: list[str] = view["warnings"]
        if res.get("symlink"):
            warnings.append("This file is a symlink; edit its target by hand.")
            view["writable"] = False
        if view["has_secrets"] and ef.world_readable(view["mode"]):
            warnings.append(
                f"Holds secrets but is readable by other accounts (mode {view['mode']}); "
                "chmod 600 it."
            )
        if view["ignored_lines"]:
            lines = ", ".join(str(n) for n in view["ignored_lines"][:6])
            warnings.append(f"Line(s) {lines} have no '=' and are ignored by runspec.")
        dup = [e["key"] for e in view["entries"] if e.get("duplicates")]
        if dup:
            warnings.append(
                f"Set more than once (the last one wins): {', '.join(dup)}. Saving a key keeps one line."
            )
        return view

    # ── transport ──

    def _env_call(
        self,
        t: dict[str, Any],
        op: str,
        payload: dict[str, Any] | None = None,
        *,
        as_user: str = "",
    ) -> dict[str, Any]:
        """Run one worker op on the venv (``as_user`` escalates a remote call)."""
        if t["local"]:
            return worker.run(op, t["root"], payload)
        h = t["entry"]
        argv = [ef.remote_python(t["root"]), "-c", _worker_source(), op, t["root"]]
        cmd, _frame = mp.remote_command(argv, run_as=as_user, become_method="sudo")
        stdin = json.dumps(payload).encode("utf-8") if payload is not None else None
        code, out, err = self._ssh_run(  # type: ignore[attr-defined]
            str(h["ssh"]),
            cmd,
            identity_file=h.get("identityFile"),
            timeout=_SSH_TIMEOUT_S,
            stdin=stdin,
        )
        res = _parse_worker_output(out or "")
        if res is None:
            msg = prov.tail("\n".join(p for p in (err or "", out or "") if p), 3)
            return {
                "ok": False,
                "no_worker": True,
                "error": msg or f"exit {code}",
            }
        return res

    def _env_access(
        self, t: dict[str, Any], *, need_write: bool
    ) -> tuple[dict[str, Any], str]:
        """Read the file as the first account that can: the SSH login, then the
        venv owner, then root (remote only). With ``need_write`` keep going
        until an account can also write it; when none can, return the best
        readable result with ``write_error`` explaining why. Returns ``(read
        result, escalated account or "")``."""
        first = self._env_call(t, "read")
        if t["local"] or not first.get("ok"):
            if first.get("ok") and need_write and not first.get("writable"):
                first["write_error"] = (
                    f"{first.get('user') or 'You'} can't write {t['path']}."
                )
            return first, ""

        def good(r: dict[str, Any]) -> bool:
            if not r.get("ok") or not r.get("readable") or r.get("error"):
                return False
            return bool(r.get("writable")) or not need_write

        if good(first):
            return first, ""
        user = str(first.get("user") or "")
        owner = str(first.get("owner") or "")
        tiers = [u for u in (owner, "root") if u and u != user]
        tiers = list(dict.fromkeys(tiers))
        best: tuple[dict[str, Any], str] | None = None
        if first.get("readable") and not first.get("error"):
            best = (first, "")
        for tier in tiers:
            r = self._env_call(t, "read", as_user=tier)
            if good(r):
                return r, tier
            if (
                best is None
                and r.get("ok")
                and r.get("readable")
                and not r.get("error")
            ):
                best = (r, tier)
        verb = "write" if need_write and best is not None else "read"
        tried = " or ".join(tiers) or "another account"
        reason = (
            f"{user or 'The SSH login'} can't {verb} {t['path']}, and passwordless sudo "
            f"as {tried} was refused for {ef.remote_python(t['root'])}. Grant it (a scoped "
            f"NOPASSWD rule on the venv's bin/), or log in as {owner or 'the venv owner'}."
        )
        if best is not None:
            res, tier = best
            res = dict(res)
            res["writable"] = False
            res["write_error"] = reason
            return res, tier
        out = dict(first)
        out["error"] = (
            reason if first.get("readable") is False else first.get("error") or reason
        )
        return out, ""

    def _env_patch_one(
        self,
        t: dict[str, Any],
        sets: dict[str, str],
        drops: list[str],
        *,
        expect: str | None,
    ) -> dict[str, Any]:
        """Read → patch → compare-and-swap write for one venv. ``expect`` (the
        editor's hash) must match the file now; ``None`` patches whatever is
        there (a bulk change touches only its own key)."""
        res, as_user = self._env_access(t, need_write=True)
        if not res.get("ok") or not res.get("readable") or res.get("error"):
            return {
                "ok": False,
                "error": res.get("error") or f"Can't read {t['path']}.",
            }
        if expect is not None and expect != res.get("sha"):
            return {
                "ok": False,
                "conflict": True,
                "error": f"{t['path']} changed since you opened it. Reload to see the new version.",
            }
        before = res.get("text") or ""
        try:
            after = ef.apply_changes(before, sets, drops)
        except ef.EnvFileError as exc:
            return {"ok": False, "error": str(exc)}
        changes = ef.changed_keys(before, after)
        if after == before or (not res.get("exists") and not after):
            return {
                "ok": True,
                "written": False,
                "changes": changes,
                "as_user": as_user,
            }
        if not res.get("writable"):
            return {
                "ok": False,
                "error": res.get("write_error") or f"Can't write {t['path']}.",
            }
        out = self._env_call(
            t, "write", {"text": after, "expect": res.get("sha")}, as_user=as_user
        )
        if not out.get("ok"):
            return {
                "ok": False,
                "conflict": bool(out.get("conflict")),
                "error": str(out.get("error") or "write failed"),
            }
        writer = as_user or str(out.get("user") or "")
        self._env_audit(
            "venv.env.changed",
            t,
            added=changes["added"],
            changed=changes["changed"],
            removed=changes["removed"],
            as_user=writer,
        )
        self._env_host_audit(t, changes, as_user)
        return {"ok": True, "written": True, "changes": changes, "as_user": writer}

    # ── audit ──

    def _env_audit(self, event: str, t: dict[str, Any], **fields: Any) -> None:
        append = getattr(self, "_append_tool_audit", None)
        if not callable(append):
            return
        user_fn = getattr(self, "_current_user", None)
        user = user_fn() if callable(user_fn) else ""
        try:
            append(
                prov.audit_record(
                    event,
                    utils_mod._iso_now(),
                    user=user,
                    host=t["host"],
                    path=t["path"],
                    **fields,
                )
            )
        except Exception:  # noqa: BLE001 — audit never breaks the edit
            logger.warning("envfile: console audit failed", exc_info=True)

    def _env_host_audit(
        self, t: dict[str, Any], changes: dict[str, list[str]], as_user: str
    ) -> None:
        """Best-effort ``runspec audit record venv.env.changed`` in the venv
        itself (its hash-chained ledger + syslog), as the account that wrote."""
        user_fn = getattr(self, "_current_user", None)
        by = user_fn() if callable(user_fn) else ""
        argv = [
            t["runspec_path"],
            "audit",
            "record",
            "venv.env.changed",
            "--outcome",
            "ok",
            "--target",
            f"path={t['path']}",
            *prov._audit_pairs(
                "--detail",
                {
                    "added": changes["added"],
                    "changed": changes["changed"],
                    "removed": changes["removed"],
                    "by": f"runspec-console:{by}" if by else "runspec-console",
                },
            ),
        ]
        try:
            if t["local"]:
                from .procutil import no_window_kwargs

                subprocess.run(
                    argv, capture_output=True, timeout=20, **no_window_kwargs()
                )
                return
            h = t["entry"]
            cmd, _ = mp.remote_command(argv, run_as=as_user, become_method="sudo")
            self._ssh_run(  # type: ignore[attr-defined]
                str(h["ssh"]), cmd, identity_file=h.get("identityFile"), timeout=20
            )
        except Exception:  # noqa: BLE001 — needs runspec >= 0.71 on the host; optional
            logger.info("envfile: host audit record skipped", exc_info=True)
