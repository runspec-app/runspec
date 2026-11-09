"""bridge_files.py — the Files tab's file actions.

Listing (``list_dir`` / ``list_dir_adhoc``) and copying (``start_transfer`` /
``start_fanout`` / ``start_relay``) already exist for the Transfer dialog and the
path picker; the Files tab adds:

* ``make_dir`` — a new folder inside the folder a pane shows.
* ``view_file`` — a read-only look at the end of a file (last N lines).
* ``rename_path`` / ``delete_paths`` — rename one entry, or delete files,
  symlinks and empty folders. The tab asks the operator to approve each one
  first; both are recorded in the console audit log (``files.renamed`` /
  ``files.deleted``).
* ``files_accounts`` — the accounts a pane may browse a host as: the SSH login
  and the owners of the host's venvs.

They run on this computer or a configured host, as the SSH login user (or the
console user locally), or — with ``as_user`` — as one of the host's venv owners
through ``sudo -n -u`` (the same NOPASSWD route Env files uses). ``as_user`` is
checked against :meth:`files_accounts`, so a pane can't name any other account.
Ad-hoc hosts are browse-and-copy only. None of this is an agent tool.
"""

from __future__ import annotations

import posixpath
from typing import Any

from . import bridge_utils as utils_mod
from . import provision as prov
from .bridge_const import logger

# Most entries one delete may name.
MAX_DELETE = 500


class FilesMixin:
    # ── accounts ──

    def files_accounts(self, host: str) -> dict[str, Any]:
        """``{login, accounts}``: the SSH login a pane browses ``host`` as, and
        the other accounts it may switch to (the owners of the host's venvs,
        found at discovery). Empty for this computer and unknown hosts."""
        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None or not entry.get("ssh"):
            return {"login": "", "accounts": []}
        with self._lock:  # type: ignore[attr-defined]
            records = [
                r
                for r in (getattr(self, "_runnables_cache", []) or [])
                if r.get("host") == host
            ]
        login = next((str(r["loginUser"]) for r in records if r.get("loginUser")), "")
        if not login:
            target = str(entry.get("ssh") or "")
            login = target.split("@", 1)[0] if "@" in target else ""
        owners = sorted(
            {
                str(r["venvOwner"])
                for r in records
                if r.get("venvOwner") and str(r["venvOwner"]) != login
            }
        )
        return {"login": login, "accounts": owners}

    def _files_as(self, host: str, as_user: str) -> str | None:
        """``None`` when ``as_user`` is allowed on ``host`` ('' always is),
        else the error to return."""
        if not as_user:
            return None
        if as_user in self.files_accounts(host)["accounts"]:
            return None
        return f"{as_user} isn't a venv owner on {host}"

    def _files_audit(self, event: str, host: str, as_user: str, **fields: Any) -> None:
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
                    host=host,
                    as_user=as_user,
                    **fields,
                )
            )
        except Exception:  # noqa: BLE001 — audit never breaks the action
            logger.warning("files: console audit failed", exc_info=True)

    # ── actions ──

    def make_dir(
        self, host: str, parent: str, name: str, as_user: str = ""
    ) -> dict[str, Any]:
        """Create folder ``name`` inside ``parent`` on ``host``.

        Returns ``{"path": <new folder>}`` or ``{"error": ...}``. An existing
        folder of that name is an error (no ``-p``)."""
        from .fs_browse import (
            account_error,
            as_account,
            make_local_dir,
            remote_mkdir_command,
            valid_folder_name,
        )

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        bad = self._files_as(host, as_user)
        if bad:
            return {"error": bad}
        try:
            clean = valid_folder_name(name)
            if not entry.get("ssh"):
                return {"path": make_local_dir(parent, clean)}
            if not parent:
                return {"error": "open a folder first"}
            code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                entry["ssh"],
                as_account(remote_mkdir_command(parent, clean), as_user),
                identity_file=entry.get("identityFile"),
                timeout=15,
            )
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        if code != 0:
            return {
                "error": account_error(err, as_user)
                or f"could not create {clean} in {parent} (exit {code})"
            }
        lines = out.strip().splitlines()
        return {"path": lines[-1] if lines else f"{parent.rstrip('/')}/{clean}"}

    def view_file(
        self, host: str, path: str, lines: int = 500, as_user: str = ""
    ) -> dict[str, Any]:
        """The last ``lines`` lines of ``path`` on ``host``, read-only.

        Returns ``{size, text, lines, partial, binary}`` (see
        ``fs_browse._view_payload``) or ``{"error": ...}``."""
        from .fs_browse import (
            account_error,
            as_account,
            parse_view_output,
            remote_view_command,
            view_local,
        )

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        bad = self._files_as(host, as_user)
        if bad:
            return {"error": bad}
        if not path:
            return {"error": "no file selected"}
        try:
            if not entry.get("ssh"):
                return view_local(path, lines)
            code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                entry["ssh"],
                as_account(remote_view_command(path, lines), as_user),
                identity_file=entry.get("identityFile"),
                timeout=30,
            )
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        if code != 0:
            return {
                "error": account_error(err, as_user)
                or f"could not read {path} (missing, not a file, or no permission)"
            }
        return parse_view_output(out, lines)

    def rename_path(
        self, host: str, parent: str, old: str, new: str, as_user: str = ""
    ) -> dict[str, Any]:
        """Rename ``old`` to ``new`` inside ``parent`` on ``host``. Never
        overwrites. Returns ``{"path": <new path>}`` or ``{"error": ...}``."""
        from .fs_browse import (
            account_error,
            as_account,
            remote_rename_command,
            rename_local,
            valid_folder_name,
        )

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        bad = self._files_as(host, as_user)
        if bad:
            return {"error": bad}
        try:
            old_c, new_c = valid_folder_name(old), valid_folder_name(new)
            if not parent:
                return {"error": "open a folder first"}
            if old_c == new_c:
                return {"error": "the new name is the same as the old one"}
            if not entry.get("ssh"):
                path = rename_local(parent, old_c, new_c)
            else:
                code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                    entry["ssh"],
                    as_account(remote_rename_command(parent, old_c, new_c), as_user),
                    identity_file=entry.get("identityFile"),
                    timeout=15,
                )
                if code != 0:
                    return {
                        "error": account_error(err, as_user)
                        or f"could not rename {old_c} (exit {code})"
                    }
                path = posixpath.join(parent, new_c)
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        self._files_audit(
            "files.renamed", host, as_user, path=parent, old=old_c, new=new_c
        )
        return {"path": path}

    def delete_paths(
        self, host: str, paths: list[str], as_user: str = ""
    ) -> dict[str, Any]:
        """Delete files, symlinks and empty folders on ``host``. A folder that
        isn't empty is left alone and reported. Returns ``{deleted: [...],
        failed: [{path, error}]}`` or ``{"error": ...}``."""
        from .fs_browse import (
            account_error,
            as_account,
            delete_local,
            parse_delete_output,
            remote_delete_command,
        )

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        bad = self._files_as(host, as_user)
        if bad:
            return {"error": bad}
        clean = [str(p) for p in (paths or []) if str(p).strip()]
        if not clean:
            return {"error": "nothing selected"}
        if len(clean) > MAX_DELETE:
            return {"error": f"at most {MAX_DELETE} entries at a time"}
        for p in clean:
            name = p.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if name in ("", ".", "..") or "\n" in p or "\0" in p:
                return {"error": f"won't delete {p!r}"}
        try:
            if not entry.get("ssh"):
                res = delete_local(clean)
            else:
                if not all(p.startswith("/") for p in clean):
                    return {"error": "paths must be absolute"}
                code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                    entry["ssh"],
                    as_account(remote_delete_command(clean), as_user),
                    identity_file=entry.get("identityFile"),
                    timeout=60,
                )
                if code != 0 and not out.strip():
                    return {
                        "error": account_error(err, as_user)
                        or f"could not delete (exit {code})"
                    }
                res = parse_delete_output(out, clean)
        except OSError as e:
            return {"error": str(e)}
        if res["deleted"]:
            self._files_audit(
                "files.deleted",
                host,
                as_user,
                paths=res["deleted"],
                failed=len(res["failed"]),
            )
        return res
