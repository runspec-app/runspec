"""bridge_files.py — the Files tab's two small file actions.

Listing (``list_dir`` / ``list_dir_adhoc``) and copying (``start_transfer`` /
``start_fanout`` / ``start_relay``) already exist for the Transfer dialog and the
path picker; the Files tab adds:

* ``make_dir`` — a new folder inside the folder a pane shows.
* ``view_file`` — a read-only look at the end of a file (last N lines).

Both run on this computer or a configured host, as the SSH login user (or the
console user locally) — the same identity the panes list as. Ad-hoc hosts are
browse-and-copy only.
"""

from __future__ import annotations

from typing import Any


class FilesMixin:
    def make_dir(self, host: str, parent: str, name: str) -> dict[str, Any]:
        """Create folder ``name`` inside ``parent`` on ``host``.

        Returns ``{"path": <new folder>}`` or ``{"error": ...}``. An existing
        folder of that name is an error (no ``-p``)."""
        from .fs_browse import make_local_dir, remote_mkdir_command, valid_folder_name

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        try:
            clean = valid_folder_name(name)
            if not entry.get("ssh"):
                return {"path": make_local_dir(parent, clean)}
            if not parent:
                return {"error": "open a folder first"}
            code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                entry["ssh"],
                remote_mkdir_command(parent, clean),
                identity_file=entry.get("identityFile"),
                timeout=15,
            )
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        if code != 0:
            return {
                "error": err.strip()
                or f"could not create {clean} in {parent} (exit {code})"
            }
        lines = out.strip().splitlines()
        return {"path": lines[-1] if lines else f"{parent.rstrip('/')}/{clean}"}

    def view_file(self, host: str, path: str, lines: int = 500) -> dict[str, Any]:
        """The last ``lines`` lines of ``path`` on ``host``, read-only.

        Returns ``{size, text, lines, partial, binary}`` (see
        ``fs_browse._view_payload``) or ``{"error": ...}``."""
        from .fs_browse import parse_view_output, remote_view_command, view_local

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            return {"error": f"unknown host {host}"}
        if not path:
            return {"error": "no file selected"}
        try:
            if not entry.get("ssh"):
                return view_local(path, lines)
            code, out, err = self._ssh_run(  # type: ignore[attr-defined]
                entry["ssh"],
                remote_view_command(path, lines),
                identity_file=entry.get("identityFile"),
                timeout=30,
            )
        except (OSError, ValueError) as e:
            return {"error": str(e)}
        if code != 0:
            return {
                "error": err.strip()
                or f"could not read {path} (missing, not a file, or no permission)"
            }
        return parse_view_output(out, lines)
